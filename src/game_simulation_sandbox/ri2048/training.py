import base64
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import PIL.Image

import tensorflow as tf
import tf_agents
import tqdm

from tf_agents.agents.dqn import dqn_agent
from tf_agents.agents import TFAgent
from tf_agents.drivers import dynamic_step_driver
from tf_agents.environments import suite_gym
from tf_agents.environments import tf_py_environment
from tf_agents.eval import metric_utils
from tf_agents.metrics import tf_metrics
from tf_agents.networks import actor_distribution_network
from tf_agents.networks import q_network
from tf_agents.networks import network
from tf_agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.trajectories import trajectory
from tf_agents.utils import common
from tf_agents.trajectories import time_step as ts
from tf_agents.policies import actor_policy
from tf_agents.specs import tensor_spec

import matplotlib.pyplot as pl

from . import environment

tf.compat.v1.enable_v2_behavior()


class ActionNet(network.Network):
    def __init__(self, input_tensor_spec, output_tensor_spec):
        super(ActionNet, self).__init__(
            input_tensor_spec=input_tensor_spec,
            state_spec=(),
            name='ActionNet')
        self._output_tensor_spec = output_tensor_spec
        self._sub_layers = [
            tf.keras.layers.Dense(32, activation=tf.keras.activations.relu),
            tf_agents.keras_layers.InnerReshape((16, 32), (16 * 32,)),
            tf.keras.layers.Dense(32, activation=tf.keras.activations.relu),
            tf.keras.layers.Dense(4, activation=tf.keras.activations.softmax),
        ]

    def call(self, observations, step_type, network_state):
        output = tf.cast(observations, dtype=tf.float32)
        for layer in self._sub_layers:
            output = layer(output)
        actions = tf.reshape(output, [-1] + self._output_tensor_spec.shape.as_list())
        # Scale and shift actions to the correct range if necessary.
        return actions, network_state


def make_actor_policy(input_tensor_spec, action_spec):
    time_step_spec = ts.time_step_spec(input_tensor_spec)
    action_net = ActionNet(input_tensor_spec, action_spec)
    my_actor_policy = actor_policy.ActorPolicy(
        time_step_spec=time_step_spec,
        action_spec=action_spec,
        actor_network=action_net)
    return my_actor_policy


def make_agent():
    env_name = "CartPole-v0"
    num_iterations = 1000
    collect_episodes_per_iteration = 5
    replay_buffer_capacity = 2000

    fc_layer_params = (32, 32, 32, 4)

    learning_rate = 1e-3
    log_interval = 25
    num_eval_episodes = 10
    eval_interval = 5

    train_env = environment.make_tf_environment()
    eval_env = environment.make_tf_environment()

    actor_net = tf_agents.networks.Sequential(
        layers=[
            # tf.keras.layers.Flatten(),
            tf.keras.layers.Dense(32, activation=tf.keras.activations.relu),
            tf_agents.keras_layers.InnerReshape((16, 32), (16 * 32,)),
            tf.keras.layers.Dense(32, activation=tf.keras.activations.relu),
            tf.keras.layers.Dense(4, activation=tf.keras.activations.softmax),
        ],
        input_spec=train_env.observation_spec(),
    )

    # print(actor_net.losses)
    # actor_net.summary()

    actor_net2 = actor_distribution_network.ActorDistributionNetwork(
        train_env.observation_spec(),
        train_env.action_spec(),
        fc_layer_params=fc_layer_params)

    # actor_policy = actor_policy.ActorPolicy(
    #     time_step_spec = train_env.time_step_spec,
    #     action_spec =
    #     train_env.action_spec(),
    #     actor_network = actor_net,
    #     training = True,
    #     clip = False,
    # )

    q_net = q_network.QNetwork(
        train_env.observation_spec(),
        train_env.action_spec(),
        fc_layer_params=(100, 100, 100))

    optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate=learning_rate)

    train_step_counter = tf.compat.v2.Variable(0)

    tf_agent = dqn_agent.DqnAgent(
        train_env.time_step_spec(),
        train_env.action_spec(),
        q_network=q_net,
        optimizer=optimizer,
        train_step_counter=train_step_counter,
    )
    tf_agent.initialize()

    avg_return = compute_avg_return(train_env, tf_agent.policy, 5)
    print('Average return:\n', avg_return)

    eval_policy = tf_agent.policy
    collect_policy = tf_agent.collect_policy

    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        data_spec=tf_agent.collect_data_spec,
        batch_size=train_env.batch_size,
        max_length=replay_buffer_capacity,
    )

    # (Optional) Optimize by wrapping some of the code in a graph using TF function.
    tf_agent.train = common.function(tf_agent.train)

    # Reset the train step
    tf_agent.train_step_counter.assign(0)

    # Evaluate the agent's policy once before training.
    avg_return = compute_avg_return(eval_env, tf_agent.policy, num_eval_episodes)
    returns = [avg_return]

    batch_size = 64
    dataset = replay_buffer.as_dataset(num_parallel_calls=3,
                                       sample_batch_size=batch_size,
                                       num_steps=2).prefetch(3)
    iterator = iter(dataset)
    collect_episode(
        train_env,
        tf_agent.collect_policy,
        batch_size,
        replay_buffer,
    )

    for _ in tqdm.tqdm(range(num_iterations)):
        # Collect a few episodes using collect_policy and save to the replay buffer.
        collect_episode(
            train_env,
            tf_agent.collect_policy,
            collect_episodes_per_iteration,
            replay_buffer,
        )

        # Use data from the buffer and update the agent's network.
        experience, unused_info = next(iterator)
        train_loss = tf_agent.train(experience).loss
        step = tf_agent.train_step_counter.numpy()

        if step % log_interval == 0:
            print("\nstep = {0}: loss = {1}".format(step, train_loss))

        if step % eval_interval == 0:
            avg_return = compute_avg_return(
                eval_env, tf_agent.policy, num_eval_episodes
            )
            print("\nstep = {0}: Average Return = {1}".format(step, avg_return))
            returns.append(avg_return)

        steps = np.arange(0, len(returns)) * eval_interval
        pl.clf()
        pl.plot(steps, returns, marker="o")
        pl.ylabel("Average Return")
        pl.xlabel("Step")
        pl.savefig("training.pdf")
        pl.savefig("training.png", dpi=150)


def compute_avg_return(env, policy, num_episodes=10):
    total_return = 0.0
    for _ in range(num_episodes):

        time_step = env.reset()
        episode_return = 0.0

        while not time_step.is_last():
            action_step = policy.action(time_step)
            time_step = env.step(action_step.action)
            episode_return += time_step.reward
        total_return += episode_return

    avg_return = total_return / num_episodes
    return avg_return.numpy()[0]


def collect_episode(env, policy, num_episodes, replay_buffer):
    episode_counter = 0
    env.reset()

    while episode_counter < num_episodes:
        time_step = env.current_time_step()
        action_step = policy.action(time_step)
        next_time_step = env.step(action_step.action)
        traj = trajectory.from_transition(time_step, action_step, next_time_step)

        # Add trajectory to the replay buffer
        replay_buffer.add_batch(traj)

        if traj.is_boundary():
            episode_counter += 1
