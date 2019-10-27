# Most of the code in this model was taken from: https://github.com/wenxinxu/resnet-in-tensorflow/blob/master/resnet.py

import tensorflow as tf
from coordnet_model import coord_conv
import numpy as np
import yaml
import os
import datetime
import time

BN_EPSILON = 0.001


class ResNetModel:
    def __init__(self, prefix, config):
        self.prefix = '{}_resnet'.format(prefix)
        self.config = config
        self.use_coordnet = self.config['network']['use_coordnet']
        self.l2_regularization_coefficient = self.config['train']['l2_regularization_coefficient']

    def activation_summary(self, x):
        """
        :param x: A Tensor
        :return: Add histogram summary and scalar summary of the sparsity of the tensor
        """
        tensor_name = x.op.name
        tf.summary.histogram(tensor_name + '/activations', x)
        tf.summary.scalar(tensor_name + '/sparsity', tf.nn.zero_fraction(x))

    def create_variables(self, name, shape, initializer=tf.contrib.layers.xavier_initializer(), is_fc_layer=False):
        """
        :param name: A string. The name of the new variable
        :param shape: A list of dimensions
        :param initializer: User Xavier as default.
        :param is_fc_layer: Want to create fc layer variable? May use different weight_decay for fc
        layers.
        :return: The created variable
        """

        ## TODO: to allow different weight decay to fully connected layer and conv layer
        regularizer = tf.contrib.layers.l2_regularizer(scale=self.l2_regularization_coefficient)

        new_variables = tf.get_variable(name, shape=shape, initializer=initializer,
                                        regularizer=regularizer)
        return new_variables

    def output_layer(self, input_layer, num_labels):
        """
        :param input_layer: 2D tensor
        :param num_labels: int. How many output labels in total? (10 for cifar10 and 100 for cifar100)
        :return: output layer Y = WX + B
        """
        input_dim = input_layer.get_shape().as_list()[-1]
        fc_w = self.create_variables(name='fc_weights', shape=[input_dim, num_labels], is_fc_layer=True,
                                initializer=tf.uniform_unit_scaling_initializer(factor=1.0))
        fc_b = self.create_variables(name='fc_bias', shape=[num_labels], initializer=tf.zeros_initializer())

        fc_h = tf.matmul(input_layer, fc_w) + fc_b
        return fc_h

    def batch_normalization_layer(self, input_layer, dimension):
        """
        Helper function to do batch normalziation
        :param input_layer: 4D tensor
        :param dimension: input_layer.get_shape().as_list()[-1]. The depth of the 4D tensor
        :return: the 4D tensor after being normalized
        """
        mean, variance = tf.nn.moments(input_layer, axes=[0, 1, 2])
        beta = tf.get_variable('beta', dimension, tf.float32,
                               initializer=tf.constant_initializer(0.0, tf.float32))
        gamma = tf.get_variable('gamma', dimension, tf.float32,
                                initializer=tf.constant_initializer(1.0, tf.float32))
        bn_layer = tf.nn.batch_normalization(input_layer, mean, variance, beta, gamma, BN_EPSILON)

        return bn_layer

    def conv_bn_relu_layer(self, input_layer, filter_shape, stride):
        """
        A helper function to conv, batch normalize and relu the input tensor sequentially
        :param input_layer: 4D tensor
        :param filter_shape: list. [filter_height, filter_width, filter_depth, filter_number]
        :param stride: stride size for conv
        :return: 4D tensor. Y = Relu(batch_normalize(conv(X)))
        """

        out_channel = filter_shape[-1]

        if self.use_coordnet:
            conv_layer = coord_conv(55, 111, False, input_layer, filter_shape[-1], filter_shape[0:2], stride,
                                    padding='same', use_bias=True, name='{}_conv1'.format(self.prefix))
        else:
            filter = self.create_variables(name='conv', shape=filter_shape)
            conv_layer = tf.nn.conv2d(input_layer, filter, strides=[1, stride, stride, 1], padding='SAME')
        bn_layer = self.batch_normalization_layer(conv_layer, out_channel)

        output = tf.nn.relu(bn_layer)
        return output

    def bn_relu_conv_layer(self, input_layer, filter_shape, stride):
        """
        A helper function to batch normalize, relu and conv the input layer sequentially
        :param input_layer: 4D tensor
        :param filter_shape: list. [filter_height, filter_width, filter_depth, filter_number]
        :param stride: stride size for conv
        :return: 4D tensor. Y = conv(Relu(batch_normalize(X)))
        """

        in_channel = input_layer.get_shape().as_list()[-1]

        bn_layer = self.batch_normalization_layer(input_layer, in_channel)
        relu_layer = tf.nn.relu(bn_layer)

        filter = self.create_variables(name='conv', shape=filter_shape)
        conv_layer = tf.nn.conv2d(relu_layer, filter, strides=[1, stride, stride, 1], padding='SAME')
        return conv_layer

    def residual_block(self, input_layer, output_channel, first_block=False):
        """
        Defines a residual block in ResNet
        :param input_layer: 4D tensor
        :param output_channel: int. return_tensor.get_shape().as_list()[-1] = output_channel
        :param first_block: if this is the first residual block of the whole network
        :return: 4D tensor.
        """
        input_channel = input_layer.get_shape().as_list()[-1]

        # When it's time to "shrink" the image size, we use stride = 2
        if input_channel * 2 == output_channel:
            increase_dim = True
            stride = 2
        elif input_channel == output_channel:
            increase_dim = False
            stride = 1
        else:
            raise ValueError('Output and input channel does not match in residual blocks!!!')

        # The first conv layer of the first residual block does not need to be normalized and relu-ed.
        with tf.variable_scope('conv1_in_block'):
            if first_block:
                filter = self.create_variables(name='conv', shape=[3, 3, input_channel, output_channel])
                conv1 = tf.nn.conv2d(input_layer, filter=filter, strides=[1, 1, 1, 1], padding='SAME')
            else:
                conv1 = self.bn_relu_conv_layer(input_layer, [3, 3, input_channel, output_channel], stride)

        with tf.variable_scope('conv2_in_block'):
            conv2 = self.bn_relu_conv_layer(conv1, [3, 3, output_channel, output_channel], 1)

        # When the channels of input layer and conv2 does not match, we add zero pads to increase the
        #  depth of input layers
        if increase_dim is True:
            pooled_input = tf.nn.avg_pool(input_layer, ksize=[1, 2, 2, 1],
                                          strides=[1, 2, 2, 1], padding='VALID')
            padding_w = input_layer.shape[1].value % 2
            padding_h = input_layer.shape[2].value % 2
            padded_input = tf.pad(pooled_input, [[0, 0], [padding_w, 0], [padding_h, 0], [input_channel // 2,
                                                                                          input_channel // 2]])
        else:
            padded_input = input_layer

        output = conv2 + padded_input
        return output

    def predict(self, input_tensor_batch, n, reuse):
        """
        The main function that defines the ResNet. total layers = 1 + 2n + 2n + 2n +1 = 6n + 2
        :param input_tensor_batch: 4D tensor
        :param n: num_residual_blocks
        :param reuse: To build train graph, reuse=False. To build validation graph and share weights
        with train graph, resue=True
        :return: last layer in the network. Not softmax-ed
        """

        layers = []
        with tf.variable_scope('conv0', reuse=reuse):
            if self.use_coordnet:
                first_filter_shape = [3, 3, 3, 16]
            else:
                first_filter_shape = [3, 3, 1, 16]
            conv0 = self.conv_bn_relu_layer(input_tensor_batch, first_filter_shape, 1)
            self.activation_summary(conv0)
            layers.append(conv0)

        for i in range(n):
            with tf.variable_scope('conv1_%d' % i, reuse=reuse):
                if i == 0:
                    conv1 = self.residual_block(layers[-1], 16, first_block=True)
                else:
                    conv1 = self.residual_block(layers[-1], 16)
                self.activation_summary(conv1)
                layers.append(conv1)

        for i in range(n):
            with tf.variable_scope('conv2_%d' % i, reuse=reuse):
                conv2 = self.residual_block(layers[-1], 32)
                self.activation_summary(conv2)
                layers.append(conv2)

        for i in range(n):
            with tf.variable_scope('conv3_%d' % i, reuse=reuse):
                conv3 = self.residual_block(layers[-1], 64)
                layers.append(conv3)
            # assert conv3.get_shape().as_list()[1:] == [8, 8, 64]

        with tf.variable_scope('fc', reuse=reuse):
            in_channel = layers[-1].get_shape().as_list()[-1]
            bn_layer = self.batch_normalization_layer(layers[-1], in_channel)
            relu_layer = tf.nn.relu(bn_layer)
            global_pool = tf.reduce_mean(relu_layer, [1, 2])

            assert global_pool.get_shape().as_list()[-1:] == [64]
            output = self.output_layer(global_pool, 10)
            layers.append(output)

        return layers[-1]

    def test_graph(self, train_dir='logs'):
        """
        Run this function to look at the graph structure on tensorboard. A fast way!
        :param train_dir:
        """
        if self.use_coordnet:
            input_tensor = tf.constant(np.ones([111, 55, 32, 3]), dtype=tf.float32)
        else:
            input_tensor = tf.constant(np.ones([128, 32, 32, 1]), dtype=tf.float32)
        result = self.predict(input_tensor, 2, reuse=False)
        init = tf.initialize_all_variables()
        sess = tf.Session()
        sess.run(init)
        summary_writer = tf.summary.FileWriter(train_dir, graph=sess.graph)


if __name__ == '__main__':
    # read the config
    config_path = os.path.join(os.getcwd(), 'data/config/reward_config.yml')
    with open(config_path, 'r') as yml_file:
        config = yaml.load(yml_file)
        print('------------ Config ------------')
        print(yaml.dump(config))
    model_name = "renset_" + datetime.datetime.fromtimestamp(time.time()).strftime('%Y_%m_%d_%H_%M_%S')
    resnet = ResNetModel(model_name, config)
    resnet.test_graph()
