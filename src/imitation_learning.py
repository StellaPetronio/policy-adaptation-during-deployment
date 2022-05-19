from turtle import forward
import torch
import os
from tqdm import tqdm
import torch.nn as nn
import numpy as np

from arguments import parse_args
from agent.agent import make_agent
import utils
from eval import init_env
from logger import Logger


def evaluate(agent, env, args, buffer=None, step=None, L=None): # OK
    """Evaluate agent on env, storing obses, actions and next obses in buffer if any"""

    ep_rewards = []
    obses, actions, next_obses = [], [], []

    for i in range(args.num_rollouts):
        obs = env.reset()
        done = False
        episode_reward = 0
        step = 0
        rewards = []
        agent.train()

        while not done:
            # Take a step
            with utils.eval_mode(agent):
                action = agent.select_action(obs)
            next_obs, reward, done, info, _, _ = env.step(action, rewards)
            episode_reward += reward
            if L and step:
                L.log('eval/episode_reward', episode_reward, step)
            # Save data into replay buffer
            if buffer:
                buffer.add(obs, action, next_obs)
            obses.append(obs)
            actions.append(action)
            next_obses.append(next_obs)
            obs = next_obs
            step += 1

    if L and step:
        L.dump(step)

    return np.array(ep_rewards), obses, actions, next_obses


def collect_expert_samples(agent, env, args, label): # OK
    """Collect samples for Imitation learning training
       Args : - agent : expert RL agent trained on env
              - env : dm_control environment
              - args
              - label : env specificity, eg. the cartmass
       """
    # Create replay buffer with label
    buffer = utils.SimpleBuffer(
        obs_shape=env.observation_space.shape,
        action_shape=env.action_space.shape,
        capacity=args.train_steps,
        batch_size=args.batch_size,
        label=label
    )

    ep_rewards, _, _, _ = evaluate(agent, env, args, buffer)
    return buffer, ep_rewards.mean(), ep_rewards.std()

def relabel(obses, expert): # OK
    """Relabel observations with expert agent"""
    with utils.eval_mode(expert):
        actions_new = []
        for obs in obses:
            actions_new.append(expert.select_action(obs))
    return actions_new

def load_agent(label, action_shape, args): # OK
    """Load model from directory"""

    work_dir = args.work_dir + "_" + str(label)
    L = Logger(work_dir, use_tb=True, config='il')
    model_dir = utils.make_dir(os.path.join(work_dir, 'model'))
    print(f'Load agent from {work_dir}')

    # Prepare agent
    assert torch.cuda.is_available(), 'must have cuda enabled'
    cropped_obs_shape = (3 * args.frame_stack, 84, 84)
    agent = make_agent(
        obs_shape=cropped_obs_shape,
        action_shape=action_shape,
        args=args
    )
    agent.load(model_dir, args.pad_checkpoint)

    return agent, L

class DomainSpecific(nn.Module):
    """MLP specific domain network."""
    def __init__(self, dynamics_shape, hidden_dim, output_dim):
        super().__init__()
        
        self.specific = nn.Sequential(nn.Linear(dynamics_shape, hidden_dim), nn.ReLu(),
                                      nn.Linear(hidden_dim, output_dim))
    
    def forward(self, gt):
         
        res = self.specific(gt)
        
        return res
    
def main(args):

    # TODO we need the following directories -> args.work_dir + _{label} or _normal


    # TODO better practise than lists
    labels = [0.4, 0.3, 0.2, 0.15]
    # Define 4 envts
    print("-"*60)
    print("Define environment")
    envs = []
    # TODO collect masses
    for mass in labels:
        envs.append(init_env(args, mass))

    # Load expert agents
    print("-" * 60)
    print("Load experts")
    experts = []
    loggers = []
    for mass in labels:
        # All envs have should have the same action space shape
        agent, logger = load_agent(mass, envs[0].action_space.shape, args)
        experts.append(agent)
        loggers.append(logger)

    # Collect samples from 4 RL agents
    print("-" * 60)
    print("Fill in buffers")
    buffers = [] # save data for IL
    stats_expert = dict() # save score of trained RL agents on corresponding environments
    stats_il = {k:[] for k in labels} # save score of Il agents

    # Initialize buffers by collecting experts data and collect their performance in the meantime
    for expert, mass, env in zip(experts, labels, envs) :
        buffer, mean, std = collect_expert_samples(expert, env, args, mass)
        buffers.append(buffer)
        stats_expert[mass] = [mean, std]

    # TODO create 4 IL agents sharing domain generic part
    print("-" * 60)
    print("Create IL agents")
    il_agents = []

    # Train the four IL agents with DAgger algorithm
    print("-" * 60)
    print("Train IL agents")

    for it in range(args.n_iter): # number of dagger iterations
        print("\n\n********** Training %i ************"%it)

        # Train 4 Il agents policies
        for step in range(args.il_steps):

            # Sample data
            obses, next_obses, preds, gts = [], [], [], []

            # Forward pass for all agents
            for agent, buffer in zip(il_agents, buffers):
                obs, action, next_obs = buffer.sample() # sample a batch
                # TODO add mass as input
                action_pred = agent.select_action(obs) # evaluate agent in train mode
                # TODO inverse dynamics
                obses.append(obs)
                preds.append(action_pred)
                gts.append(action)
                next_obses.append(next_obs)

            # Backward pass
            for agent, L, obs, next_obs, pred, gt in zip(il_agents, loggers, obses, next_obses, preds, gts):
                agent.update(obs, next_obs, pred, gt, step, L)

        # Evaluate - Perform IL agent policy rollouts
        print("\n\n********** Evaluation and relabeling %i ************" % it)
        for agent, expert, logger, env, buffer, mass in zip(il_agents, experts, loggers, envs, buffers, labels):
            rewards, obses, actions, next_obses = evaluate(agent, env, args, L=logger, step=step) # evaluate agent on environment
            stats_il[mass].append([rewards.mean(), rewards.std()]) # save intermediary score
            print(f'Performance of agent on mass {mass} : {rewards.mean()} +/- {rewards.std()}')
            actions_new = relabel(obses, expert)
            buffer.add_batch(obses, actions_new, next_obses)


    # Evaluate IL agents on environments

    # Baseline agent -> PAD
    pad_agent = load_agent("pad", envs[0].action_space.shape, args)
    pad_stats = dict()

    for env, label in zip(envs, labels) :
        rewards, _, _, _ = evaluate(pad_agent, env, args)
        pad_stats[label] = [rewards.mean(), rewards.std()]

    for label in labels :
        print("-"*60)
        print(f'Mass of {label}')
        print(f'Baseline performance: {pad_stats[label][0]} +/- {pad_stats[label][1]}')
        print(f'Expert performance : {stats_expert[label][0]} +/- {stats_expert[label][1]}')
        print(f'Imitation learning agent with dagger performance : {stats_il[label][-1][0]} +/- {stats_il[label][-1][0]}')

if __name__ == '__main__':
    args = parse_args()
    main(args)