# Copyright 2022 The Deep RL Zoo Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""
From the paper "Policy Gradient Methods for Reinforcement Learning with Function Approximation"
https://proceedings.neurips.cc/paper/1999/file/464d828b85b0bed98e80ade0a5c43b0f-Paper.pdf.
"""
from absl import app
from absl import flags
from absl import logging
import numpy as np
import torch

# pylint: disable=import-error
from deep_rl_zoo.networks.policy import ActorMlpNet
from deep_rl_zoo.reinforce import agent
from deep_rl_zoo import replay as replay_lib
from deep_rl_zoo.checkpoint import PyTorchCheckpoint
from deep_rl_zoo import main_loop
from deep_rl_zoo import gym_env
from deep_rl_zoo import greedy_actors


FLAGS = flags.FLAGS
flags.DEFINE_string(
    'environment_name',
    'CartPole-v1',
    'Classic control tasks name like CartPole-v1, LunarLander-v2, MountainCar-v0, Acrobot-v1.',
)
flags.DEFINE_bool('normalize_returns', False, 'Normalize episode returns, default off.')
flags.DEFINE_bool('clip_grad', False, 'Clip gradients, default off.')
flags.DEFINE_float('max_grad_norm', 40.0, 'Max gradients norm when do gradients clip.')
flags.DEFINE_float('learning_rate', 0.0005, 'Learning rate.')
flags.DEFINE_float('discount', 0.99, 'Discount rate.')
flags.DEFINE_integer('num_iterations', 2, 'Number of iterations to run.')
flags.DEFINE_integer('num_train_frames', int(5e5), 'Number of training env steps to run per iteration.')
flags.DEFINE_integer('num_eval_frames', int(1e5), 'Number of evaluation env steps to run per iteration.')
flags.DEFINE_integer('seed', 1, 'Runtime seed.')
flags.DEFINE_bool('tensorboard', True, 'Use Tensorboard to monitor statistics, default on.')
flags.DEFINE_integer(
    'debug_screenshots_frequency',
    0,
    'Take screenshots every N episodes and log to Tensorboard, default 0 no screenshots.',
)
flags.DEFINE_string('tag', '', 'Add tag to Tensorboard log file.')
flags.DEFINE_string('results_csv_path', 'logs/reinforce_classic_results.csv', 'Path for CSV log file.')
flags.DEFINE_string('checkpoint_dir', '', 'Path for checkpoint directory.')


def main(argv):
    """Trains REINFORCE agent on classic control tasks."""
    del argv
    runtime_device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    logging.info(f'Runs REINFORCE agent on {runtime_device}')
    random_state = np.random.RandomState(FLAGS.seed)  # pylint: disable=no-member
    torch.manual_seed(FLAGS.seed)
    if torch.backends.cudnn.enabled:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    # Create environment.
    def environment_builder():
        return gym_env.create_classic_environment(
            env_name=FLAGS.environment_name,
            seed=random_state.randint(1, 2**32),
        )

    train_env = environment_builder()
    eval_env = environment_builder()

    logging.info('Environment: %s', FLAGS.environment_name)
    logging.info('Action spec: %s', train_env.action_space.n)
    logging.info('Observation spec: %s', train_env.observation_space.shape[0])

    input_shape = train_env.observation_space.shape[0]
    num_actions = train_env.action_space.n

    # Create policy network and optimizer
    policy_network = ActorMlpNet(input_shape=input_shape, num_actions=num_actions)
    policy_optimizer = torch.optim.Adam(policy_network.parameters(), lr=FLAGS.learning_rate)

    # Test network output.
    obs = train_env.reset()
    pi_logits = policy_network(torch.from_numpy(obs[None, ...]).float()).pi_logits
    assert pi_logits.shape == (1, num_actions)

    # Create reinforce agent instance
    train_agent = agent.Reinforce(
        policy_network=policy_network,
        policy_optimizer=policy_optimizer,
        discount=FLAGS.discount,
        transition_accumulator=replay_lib.TransitionAccumulator(),
        normalize_returns=FLAGS.normalize_returns,
        clip_grad=FLAGS.clip_grad,
        max_grad_norm=FLAGS.max_grad_norm,
        device=runtime_device,
    )

    # Create evaluation agent instnace
    eval_agent = greedy_actors.PolicyGreedyActor(
        network=policy_network,
        device=runtime_device,
        name='REINFORCE-greedy',
    )

    # Setup checkpoint.
    checkpoint = PyTorchCheckpoint(
        environment_name=FLAGS.environment_name, agent_name='REINFORCE', save_dir=FLAGS.checkpoint_dir
    )
    checkpoint.register_pair(('policy_network', policy_network))

    # Run the training and evaluation for N iterations.
    main_loop.run_single_thread_training_iterations(
        num_iterations=FLAGS.num_iterations,
        num_train_frames=FLAGS.num_train_frames,
        num_eval_frames=FLAGS.num_eval_frames,
        network=policy_network,
        train_agent=train_agent,
        train_env=train_env,
        eval_agent=eval_agent,
        eval_env=eval_env,
        checkpoint=checkpoint,
        csv_file=FLAGS.results_csv_path,
        tensorboard=FLAGS.tensorboard,
        tag=FLAGS.tag,
        debug_screenshots_frequency=FLAGS.debug_screenshots_frequency,
    )


if __name__ == '__main__':
    app.run(main)
