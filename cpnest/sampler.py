from __future__ import division
import sys
import os
import logging
import numpy as np
from math import log
from collections import deque
from random import random,randrange

from . import parameter
from .proposal import DefaultProposalCycle
from . import proposal
from .cpnest import CheckPoint, RunManager
from tqdm import tqdm
from operator import attrgetter
import numpy.lib.recfunctions as rfn

import pickle
__checkpoint_flag = False


class Sampler(object):
    """
    Sampler class.
    ---------

    Initialisation arguments:

    args:
    model: :obj:`cpnest.Model` user defined model to sample

    maxmcmc:
        :int: maximum number of mcmc steps to be used in the :obj:`cnest.sampler.Sampler`

    ----------
    kwargs:

    verbose:
        :int: display debug information on screen
        Default: 0

    poolsize:
        :int: number of objects for the affine invariant sampling
        Default: 1000

    seed:
        :int: random seed to initialise the pseudo-random chain
        Default: None

    proposal:
        :obj:`cpnest.proposals.Proposal` to use
        Defaults: :obj:`cpnest.proposals.DefaultProposalCycle`)

    resume_file:
        File for checkpointing
        Default: None

    manager:
        :obj:`multiprocessing.Manager` hosting all communication objects
        Default: None
    """

    def __init__(self,
                 model,
                 maxmcmc,
                 seed        = None,
                 output      = None,
                 verbose     = False,
                 poolsize    = 1000,
                 proposal    = None,
                 resume_file = None,
                 manager     = None):

        self.seed = seed
        self.model = model
        self.initial_mcmc = maxmcmc//10
        self.maxmcmc = maxmcmc
        self.resume_file = resume_file
        self.manager = manager
        self.logLmin = self.manager.logLmin
        self.logLmax = self.manager.logLmax

        self.logger = logging.getLogger('CPNest')

        if proposal is None:
            self.proposal = DefaultProposalCycle()
        else:
            self.proposal = proposal

        self.Nmcmc              = self.initial_mcmc
        self.Nmcmc_exact        = float(self.initial_mcmc)

        self.poolsize           = poolsize
        self.evolution_points   = deque(maxlen = self.poolsize)
        self.verbose            = verbose
        self.acceptance         = 0.0
        self.sub_acceptance     = 0.0
        self.mcmc_accepted      = 0
        self.mcmc_counter       = 0
        self.initialised        = False
        self.output             = output
        self.samples            = [] # the list of samples from the mcmc chain
        self.ACLs               = [] # the history of the ACL of the chain, will be used to thin the output, if requested
        self.producer_pipe, self.thread_id = self.manager.connect_producer()

    def reset(self):
        """
        Initialise the sampler by generating :int:`poolsize` `cpnest.parameter.LivePoint`
        and distributing them according to :obj:`cpnest.model.Model.log_prior`
        """
        np.random.seed(seed=self.seed)
        for n in tqdm(range(self.poolsize), desc='SMPLR {} init draw'.format(self.thread_id),
                disable= not self.verbose, position=self.thread_id, leave=False):
            while True: # Generate an in-bounds sample
                p = self.model.new_point()
                p.logP = self.model.log_prior(p)
                if np.isfinite(p.logP): break
            p.logL=self.model.log_likelihood(p)
            if p.logL is None or not np.isfinite(p.logL):
                self.logger.warning("Received non-finite logL value {0} with parameters {1}".format(str(p.logL), str(p)))
                self.logger.warning("You may want to check your likelihood function to improve sampling")
            self.evolution_points.append(p)

        self.proposal.set_ensemble(self.evolution_points)
    
        # Now, run evolution so samples are drawn from actual prior
        for k in tqdm(range(self.poolsize), desc='SMPLR {} init evolve'.format(self.thread_id),
                disable= not self.verbose, position=self.thread_id, leave=False):
            _, p = next(self.yield_sample(-np.inf))

        if self.verbose >= 3:
            # save the poolsize as prior samples
            
            prior_samples = []
            for k in tqdm(range(self.maxmcmc), desc='SMPLR {} generating prior samples'.format(self.thread_id),
                disable= not self.verbose, position=self.thread_id, leave=False):
                _, p = next(self.yield_sample(-np.inf))
                prior_samples.append(p)
            prior_samples = rfn.stack_arrays([prior_samples[j].asnparray()
                for j in range(0,len(prior_samples))],usemask=False)
            np.savetxt(os.path.join(self.output,'prior_samples_%s.dat'%os.getpid()),
                       prior_samples.ravel(),header=' '.join(prior_samples.dtype.names),
                       newline='\n',delimiter=' ')
            self.logger.critical("Sampler process {0!s}: saved {1:d} prior samples in {2!s}".format(os.getpid(),self.maxmcmc,'prior_samples_%s.dat'%os.getpid()))
            self.prior_samples = prior_samples
        self.proposal.set_ensemble(self.evolution_points)
        self.initialised=True

    def estimate_nmcmc(self, safety=5, tau=None):
        """
        Estimate autocorrelation length of chain using acceptance fraction
        ACL = (2/acc) - 1
        multiplied by a safety margin of 5
        Uses moving average with decay time tau iterations
        (default: :int:`self.poolsize`)

        Taken from http://github.com/farr/Ensemble.jl
        """
        if tau is None: tau = self.maxmcmc/safety#self.poolsize

        if self.sub_acceptance == 0.0:
            self.Nmcmc_exact = (1.0 + 1.0/tau)*self.Nmcmc_exact
        else:
            self.Nmcmc_exact = (1.0 - 1.0/tau)*self.Nmcmc_exact + (safety/tau)*(2.0/self.sub_acceptance - 1.0)

        self.Nmcmc_exact = float(min(self.Nmcmc_exact,self.maxmcmc))
        self.Nmcmc = max(safety,int(self.Nmcmc_exact))

        return self.Nmcmc

    def produce_sample(self):
        try:
            self._produce_sample()
        except CheckPoint:
            self.logger.critical("Checkpoint excepted in sampler")
            self.checkpoint()

    def _produce_sample(self):
        """
        main loop that takes the worst :obj:`cpnest.parameter.LivePoint` and
        evolves it. Proposed sample is then sent back
        to :obj:`cpnest.NestedSampler`.
        """
        if not self.initialised:
            self.reset()

        self.counter=1
        __checkpoint_flag=False
        while True:

            if self.manager.checkpoint_flag.value:
                self.checkpoint()
                sys.exit(130)

            if self.logLmin.value==np.inf:
                break
            
            # if the nested sampler is requesting for an update
            # produce a sample for it
            if self.producer_pipe.poll():
                p = self.producer_pipe.recv()

                if p is None:
                    break
                if p == "checkpoint":
                    self.checkpoint()
                    sys.exit(130)

                self.evolution_points.append(p)
                (Nmcmc, outParam) = next(self.yield_sample(self.logLmin.value))
                # Send the sample to the Nested Sampler
                self.producer_pipe.send((self.acceptance,self.sub_acceptance,Nmcmc,outParam))
            
            # otherwise, keep on sampling from the previous boundary
            else:
                (Nmcmc, outParam) = next(self.yield_sample(self.logLmin.value))
            # Update the ensemble every now and again
            if (self.counter%(self.poolsize))==0:
                self.proposal.set_ensemble(self.evolution_points)

            self.counter += 1

        self.logger.critical("Sampler process {0!s}: MCMC samples accumulated = {1:d}".format(os.getpid(),len(self.samples)))
        self.samples.extend(self.evolution_points)
        
        if self.verbose >=3:
            
            self.mcmc_samples = rfn.stack_arrays([self.samples[j].asnparray()
                                                  for j in range(0,len(self.samples))],usemask=False)
            np.savetxt(os.path.join(self.output,'mcmc_chain_%s.dat'%os.getpid()),
                       self.mcmc_samples.ravel(),header=' '.join(self.mcmc_samples.dtype.names),
                       newline='\n',delimiter=' ')
            self.logger.critical("Sampler process {0!s}: saved {1:d} mcmc samples in {2!s}".format(os.getpid(),len(self.samples),'mcmc_chain_%s.dat'%os.getpid()))
        self.logger.critical("Sampler process {0!s} - mean acceptance {1:.3f}: exiting".format(os.getpid(), float(self.mcmc_accepted)/float(self.mcmc_counter)))
        return 0

    def checkpoint(self):
        """
        Checkpoint its internal state
        """
        self.logger.info('Checkpointing Sampler')
        with open(self.resume_file, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def resume(cls, resume_file, manager, model):
        """
        Resumes the interrupted state from a
        checkpoint pickle file.
        """
        with open(resume_file, "rb") as f:
            obj = pickle.load(f)
        obj.model   = model
        obj.manager = manager
        obj.logLmin = obj.manager.logLmin
        obj.logLmax = obj.manager.logLmax
        obj.producer_pipe , obj.thread_id = obj.manager.connect_producer()
        obj.logger.info('Resuming Sampler from ' + resume_file)
        return obj

    def __getstate__(self):
        state = self.__dict__.copy()
        # Remove the unpicklable entries.
        del state['model']
        del state['logLmin']
        del state['logLmax']
        del state['manager']
        del state['producer_pipe']
        del state['thread_id']
        return state

    def __setstate__(self, state):
        self.__dict__ = state
        self.manager = None

class MetropolisHastingsSampler(Sampler):
    """
    metropolis-hastings acceptance rule
    for :obj:`cpnest.proposal.EnembleProposal`
    """
    def yield_sample(self, logLmin):

        while True:

            sub_counter = 0
            sub_accepted = 0
            oldparam = self.evolution_points.popleft()
            logp_old = self.model.log_prior(oldparam)

            while True:

                sub_counter += 1
                newparam = self.proposal.get_sample(oldparam.copy())
                newparam.logP = self.model.log_prior(newparam)

                if newparam.logP-logp_old + self.proposal.log_J > log(random()):
                    newparam.logL = self.model.log_likelihood(newparam)
                    if newparam.logL > logLmin:
                        self.logLmax.value = max(self.logLmax.value, newparam.logL)
                        oldparam = newparam.copy()
                        logp_old = newparam.logP
                        sub_accepted+=1

                if (sub_counter >= self.Nmcmc and sub_accepted > 0 ) or sub_counter >= self.maxmcmc:
                    break

            # Put sample back in the stack, unless that sample led to zero accepted points
            self.evolution_points.append(oldparam)
            if self.verbose >=3:
                self.samples.append(oldparam)
            self.sub_acceptance = float(sub_accepted)/float(sub_counter)
            self.estimate_nmcmc()
            self.mcmc_accepted += sub_accepted
            self.mcmc_counter += sub_counter
            self.acceptance    = float(self.mcmc_accepted)/float(self.mcmc_counter)
            # Yield the new sample
            yield (sub_counter, oldparam)

class HamiltonianMonteCarloSampler(Sampler):
    """
    HamiltonianMonteCarlo acceptance rule
    for :obj:`cpnest.proposal.HamiltonianProposal`
    """
    def yield_sample(self, logLmin):

        global_lmax = self.logLmax.value

        while True:

            sub_accepted    = 0
            sub_counter     = 0
            oldparam        = self.evolution_points.pop()

            while sub_accepted == 0:

                sub_counter += 1
                newparam     = self.proposal.get_sample(oldparam.copy(), logLmin = logLmin)

                if self.proposal.log_J > np.log(random()):

                    if newparam.logL > logLmin:
                        global_lmax = max(global_lmax, newparam.logL)
                        oldparam        = newparam.copy()
                        sub_accepted   += 1

            self.evolution_points.append(oldparam)

            if self.verbose >= 3:
                self.samples.append(oldparam)

            self.sub_acceptance = float(sub_accepted)/float(sub_counter)
            self.mcmc_accepted += sub_accepted
            self.mcmc_counter  += sub_counter
            self.acceptance     = float(self.mcmc_accepted)/float(self.mcmc_counter)
            self.logLmax.value = global_lmax

            for p in self.proposal.proposals:
                p.update_time_step(self.acceptance)
                p.update_trajectory_length(self.estimate_nmcmc())
                #print(p.dt,p.L)

            yield (sub_counter, oldparam)

    def insert_sample(self, p):
        # if we did not accept, inject a new particle in the system (gran-canonical) from the prior
        # by picking one from the existing pool and giving it a random trajectory
        k = np.random.randint(self.evolution_points.maxlen)
        self.evolution_points.rotate(k)
        p  = self.evolution_points.pop()
        self.evolution_points.append(p)
        self.evolution_points.rotate(-k)
        return self.proposal.get_sample(p.copy(),logLmin=p.logL)

class SliceSampler(Sampler):
    """
    The Ensemble Slice sample from Karamanis & Beutler
    https://arxiv.org/pdf/2002.06212v1.pdf
    """
    def reset(self):
        """
        Initialise the sampler by generating :int:`poolsize` `cpnest.parameter.LivePoint`
        """
        np.random.seed(seed=self.seed)
        for n in tqdm(range(self.poolsize), desc='SMPLR {} init draw'.format(self.thread_id),
                disable= not self.verbose, position=self.thread_id, leave=False):
            while True: # Generate an in-bounds sample
                p = self.model.new_point()
                p.logP = self.model.log_prior(p)
                if np.isfinite(p.logP): break
            p.logL=self.model.log_likelihood(p)
            if p.logL is None or not np.isfinite(p.logL):
                self.logger.warning("Received non-finite logL value {0} with parameters {1}".format(str(p.logL), str(p)))
                self.logger.warning("You may want to check your likelihood function to improve sampling")
            self.evolution_points.append(p)

        self.proposal.set_ensemble(self.evolution_points)
        self.mu = len(self.evolution_points[0].names)
        self.initialised=True

    def adapt_length_scale(self):
        """
        adapts the length scale of the expansion/contraction
        following the rule in (Robbins and Monro, 1951) of Tibbits et al. (2014)
        the scale is capped from both above and below
        """
        self.Ne = max(1,self.Ne)
        ratio = self.Ne/(self.Ne+self.Nc)
        if np.abs(ratio - 0.5) > 0.05:
            self.mu *= 2.0*ratio
        if self.mu < 1e-2: self.mu = 1e-2
        elif self.mu > 1e6: self.mu = 1e6
    
    def reset_boundaries(self):
        """
        resets the boundaries and counts
        for the slicing
        """
        self.L = - np.random.uniform(0.0,1.0)
        self.R = self.L + 1.0
        self.Ne = 0.0
        self.Nc = 0.0
        
    def increase_left_boundary(self):
        """
        increase the left boundary and counts
        by one unit
        """
        self.L  = self.L - 1.0
        self.Ne = self.Ne + 1

    def increase_right_boundary(self):
        """
        increase the right boundary and counts
        by one unit
        """
        self.R  = self.R + 1.0
        self.Ne = self.Ne + 1
        
    def yield_sample(self, logLmin):

        global_lmax = self.logLmax.value
        
        while True:

            sub_accepted    = 0
            sub_counter     = 0
            oldparam        = self.evolution_points.popleft()
            
            while sub_accepted == 0:
                # Set Initial Interval Boundaries
                self.reset_boundaries()
                sub_counter += 1
                # if loglmin is -infinity, we are always going to accept
                # so pick a random vector and return it, if we are
                # within the prior bounds
                if not np.isfinite(logLmin):
                    while True:
                        direction_vector = self.proposal.get_direction(mu = self.mu)
                        if not(isinstance(direction_vector,parameter.LivePoint)):
                            direction_vector = parameter.LivePoint(oldparam.names,d=direction_vector)
                        Xprime = np.random.uniform(self.L,self.R)
                        # compute the new value of Y
                        newparam      = direction_vector * Xprime + oldparam
                        newparam.logP = self.model.log_prior(newparam)
                        if np.isfinite(newparam.logP):
                            newparam.logL = self.model.log_likelihood(newparam)
                            if newparam.logP-oldparam.logP > np.log(random()):
                                global_lmax     = max(global_lmax, newparam.logL)
                                oldparam        = newparam.copy()
                                sub_accepted   += 1
                                break
                else:
                    # pick a direction
                    while True:
                        direction_vector = self.proposal.get_direction(mu = self.mu)
                        if not(isinstance(direction_vector,parameter.LivePoint)):
                            direction_vector = parameter.LivePoint(oldparam.names,d=direction_vector)
                        if np.any(direction_vector.values):
                            break
                    Y = logLmin
                    # keep on expanding until we get outside the logLmin boundary from the left
                    # or the prior bound, whichever comes first
                    safety = 0
                    while True:
#                        print('inside the left boundary')
                        parameter_left = direction_vector * self.L + oldparam
                        if self.model.in_bounds(parameter_left):
                            if Y > self.model.log_likelihood(parameter_left):
                                break
                            else:
                                self.increase_left_boundary()
                        # if we get out of bounds, break out
                        else:
                            break

                    # keep on expanding until we get outside the logLmin boundary from the right
                    # or the prior bound, whichever comes first
                    safety = 0
                    while True:
#                        print('inside the right boundary')
                        parameter_right = direction_vector * self.R + oldparam
                        if self.model.in_bounds(parameter_right):
                            if Y > self.model.log_likelihood(parameter_right):
                                break
                            else:
                                self.increase_right_boundary()
                        # if we get out of bounds, break out
                        else:
                            break

                    # slice sample the likelihood
                    safety = 0
                    while True:
#                        print('inside the slicing')
                        # generate a new point between the boundaries we identified
                        Xprime = np.random.uniform(self.L,self.R)
                        # compute the new value of Y
                        newparam      = direction_vector * Xprime + oldparam
                        newparam.logP = self.model.log_prior(newparam)
                        if np.isfinite(newparam.logP):
                            newparam.logL = self.model.log_likelihood(newparam)
                            Yprime = newparam.logL
                            if Y < Yprime:
                                global_lmax  = max(global_lmax, newparam.logL)
                                oldparam     = newparam.copy()
                                sub_accepted += 1
                                break
                            # adapt the intervals shrinking them
                            if Xprime < 0.0:
                                self.L = Xprime
                                self.Nc = self.Nc + 1
                            else:
                                self.R = Xprime
                                self.Nc = self.Nc + 1
                        if np.abs(self.R-self.L) < 1e-7 or safety == 10: break
                        safety+=1

            self.evolution_points.append(oldparam)

            if self.verbose >= 3:
                self.samples.append(oldparam)

            self.sub_acceptance = float(sub_accepted)/float(sub_counter)
            self.mcmc_accepted += sub_accepted
            self.mcmc_counter  += sub_counter
            self.acceptance     = float(self.mcmc_accepted)/float(self.mcmc_counter)
            self.logLmax.value = global_lmax
            if np.isfinite(logLmin):
                self.adapt_length_scale()
#                print('adapted mu',self.mu,self.Ne,self.Nc,self.Ne/(self.Nc+self.Ne))
#            else:exit()

            yield (sub_counter, oldparam)
