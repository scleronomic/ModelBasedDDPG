import os
import random
import datetime
import tensorflow as tf
import yaml
import time
import numpy as np

from curriculum_manager import CurriculumManager
from hindsight_policy import HindsightPolicy
from network import Network
from replay_buffer import ReplayBuffer
from rollout_manager import RolloutManager
from saver_wrapper import SaverWrapper
from summaries_collector import SummariesCollector
from trajectory_eval import TrajectoryEval
from workspace_generation_utils import *


def run_for_config(config, print_messages):
    # set the name of the model
    model_name = config['general']['name']
    now = datetime.datetime.fromtimestamp(time.time()).strftime('%Y_%m_%d_%H_%M_%S')
    model_name = now + '_' + model_name if model_name is not None else now

    # openrave_interface = OpenraveRLInterface(config, None)
    random_seed = config['general']['random_seed']
    np.random.seed(random_seed)
    random.seed(random_seed)
    tf.set_random_seed(random_seed)

    # where we save all the outputs
    working_dir = os.getcwd()
    saver_dir = os.path.join(working_dir, 'models', model_name)
    config_copy_path = os.path.join(working_dir, 'models', model_name, 'config.yml')
    summaries_dir = os.path.join(working_dir, 'tensorboard', model_name)
    completed_trajectories_dir = os.path.join(working_dir, 'trajectories', model_name)

    # generate graph:
    network = Network(config, is_rollout_agent=False)

    # initialize replay memory
    replay_buffer = ReplayBuffer(config)
    hindsight_policy = HindsightPolicy(config, replay_buffer)

    # save model
    saver = SaverWrapper(saver_dir)
    yaml.dump(config, open(config_copy_path, 'w'))
    summaries_collector = SummariesCollector(summaries_dir, model_name)
    curriculum_manager = CurriculumManager(config, print_messages)
    rollout_manager = RolloutManager(config)

    test_results = []

    def unpack_state_batch(state_batch):
        joints = [state[0] for state in state_batch]
        poses = {p.tuple: [state[1][p.tuple] for state in state_batch] for p in network.potential_points}
        jacobians = None
        # jacobians = {p.tuple: [state[2][p.tuple] for state in state_batch] for p in network.potential_points}
        return joints, poses, jacobians

    def update_model(sess, global_step):
        batch_size = config['model']['batch_size']
        gamma = config['model']['gamma']
        goal_pose, goal_joints, workspace_image, current_state, action, reward, terminated, next_state = \
            replay_buffer.sample_batch(batch_size)

        current_joints, current_poses, current_jacobians = unpack_state_batch(current_state)
        next_joints, next_poses, next_jacobians = unpack_state_batch(next_state)

        # get the predicted q value of the next state (action is taken from the target policy)
        next_state_action_target_q = network.predict_policy_q(
            next_joints, workspace_image, goal_pose, goal_joints, sess, use_online_network=False
        )

        # compute critic label
        q_label = np.expand_dims(np.array(reward) + np.multiply(
            np.multiply(1 - np.array(terminated), gamma),
            np.squeeze(next_state_action_target_q)
        ), 1)
        max_label = np.max(q_label)
        min_label = np.min(q_label)
        limit = 1.0 / (1.0 - gamma)
        if max_label > limit:
            print 'out of range max label: {} limit: {}'.format(max_label, limit)
        if min_label < -limit:
            print 'out of range min label: {} limit: {}'.format(min_label, limit)

        # train critic given the targets
        critic_optimization_summaries, _ = network.train_critic(
            current_joints, workspace_image, goal_pose, goal_joints, action, q_label, sess
        )

        reward_optimization_summaries = None
        if config['model']['use_reward_model']:
            reward_input = np.expand_dims(np.array(reward), axis=1)
            reward_optimization_summaries, _ = network.train_reward(
                current_joints, workspace_image, goal_pose, goal_joints, action, reward_input, sess
            )

        # train actor
        actor_optimization_summaries, _ = network.train_actor(
            current_joints, workspace_image, goal_pose, goal_joints, sess
        )

        # update target networks
        network.update_target_networks(sess)

        return critic_optimization_summaries, actor_optimization_summaries, reward_optimization_summaries

    def print_state(prefix, episodes, successful_episodes, collision_episodes, max_len_episodes):
        if not print_messages:
            return
        print '{}: {}: finished: {}, successful: {} ({}), collision: {} ({}), max length: {} ({})'.format(
            datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S'), prefix, episodes,
            successful_episodes, float(successful_episodes) / episodes, collision_episodes,
            float(collision_episodes) / episodes, max_len_episodes, float(max_len_episodes) / episodes
        )

    with tf.Session(
            config=tf.ConfigProto(
                gpu_options=tf.GPUOptions(per_process_gpu_memory_fraction=config['general']['gpu_usage'])
            )
    ) as sess:
        sess.run(tf.global_variables_initializer())
        network.update_target_networks(sess)

        trajectory_eval = TrajectoryEval(config, rollout_manager, completed_trajectories_dir)

        global_step = 0
        total_episodes = episodes = successful_episodes = collision_episodes = max_len_episodes = 0
        test_episodes = test_successful_episodes = 0
        for update_index in range(config['general']['updates_cycle_count']):
            allowed_size, has_changed = curriculum_manager.get_next_parameters(test_episodes, test_successful_episodes)
            # allowed_size, has_changed = curriculum_manager.get_next_parameters(episodes, successful_episodes)
            if has_changed:
                test_episodes = test_successful_episodes = 0
                # episodes = successful_episodes = collision_episodes = max_len_episodes = 0

            # collect data
            a = datetime.datetime.now()
            rollout_manager.set_policy_weights(network.get_actor_online_weights(sess))
            episodes_per_update = config['general']['episodes_per_update']
            episode_results = rollout_manager.generate_episodes(episodes_per_update, allowed_size, True)
            for episode_result in episode_results:
                # run episode:
                status, states, actions, rewards, goal_pose, goal_joints, workspace_image = episode_result
                # at the end of episode
                hindsight_policy.append_to_replay_buffer(
                    status, states, actions, rewards, goal_pose, goal_joints, workspace_image
                )
                total_episodes += 1
                episodes += 1
                if status == 1:
                    max_len_episodes += 1
                elif status == 2:
                    collision_episodes += 1
                elif status == 3:
                    successful_episodes += 1
            b = datetime.datetime.now()
            print 'data collection took: {}'.format(b-a)
            print_state('train', episodes, successful_episodes, collision_episodes, max_len_episodes)

            # do updates
            if replay_buffer.size() > config['model']['intial_samples_before_train']:
                a = datetime.datetime.now()
                for _ in range(config['general']['model_updates_per_cycle']):
                    critic_optimization_summaries, actor_optimization_summaries, reward_optimization_summaries = \
                        update_model(sess, global_step)
                    if global_step % config['general']['write_train_summaries'] == 0:
                        summaries_collector.write_train_episode_summaries(
                            sess, global_step, episodes, successful_episodes, collision_episodes, max_len_episodes
                        )
                        summaries_collector.write_train_optimization_summaries(
                            critic_optimization_summaries, actor_optimization_summaries, reward_optimization_summaries,
                            global_step
                        )
                    global_step += 1
                b = datetime.datetime.now()
                print 'update took: {}'.format(b - a)

            # test if needed
            if update_index % config['test']['test_every_cycles'] == 0:
                eval_result = trajectory_eval.eval(global_step, allowed_size)
                test_episodes = eval_result[0]
                test_successful_episodes = eval_result[1]
                test_collision_episodes = eval_result[2]
                test_max_len_episodes = eval_result[3]
                test_mean_reward = eval_result[4]
                if print_messages:
                    print('test path allowed length {}'.format(allowed_size))
                    print_state('test', test_episodes, test_successful_episodes, test_collision_episodes,
                                test_max_len_episodes)
                    print('test mean total reward {}'.format(test_mean_reward))
                summaries_collector.write_test_episode_summaries(
                    sess, global_step, test_episodes, test_successful_episodes, test_collision_episodes,
                    test_max_len_episodes
                )
                summaries_collector.write_test_curriculum_summaries(sess, global_step, allowed_size)
                test_results.append((global_step, episodes, test_successful_episodes, allowed_size))
    rollout_manager.end()
    return test_results


if __name__ == '__main__':
    # disable tf warning
    # os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

    # read the config
    config_path = os.path.join(os.getcwd(), 'config/config.yml')
    with open(config_path, 'r') as yml_file:
        config = yaml.load(yml_file)
        print('------------ Config ------------')
        print(yaml.dump(config))

    run_for_config(config, print_messages=True)
