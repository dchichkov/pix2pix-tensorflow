from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np
import argparse
import os
import json
import glob
import random
import collections
import math
import time
import sys
from tqdm import tqdm

parser = argparse.ArgumentParser()
parser.add_argument("--input_dir", help="path to folder containing images")
parser.add_argument("--mode", required=True, choices=["train", "test", "export"])
parser.add_argument("--output_dir", required=True, help="where to put output files")
parser.add_argument("--seed", type=int)
parser.add_argument("--checkpoint", default=None, help="directory with checkpoint to resume training from or use for testing")

parser.add_argument("--max_examples", type=int, help="number of training steps (0 to disable)")
parser.add_argument("--max_steps", type=int, help="number of training steps (0 to disable)")
parser.add_argument("--max_epochs", type=int, help="number of training epochs")
parser.add_argument("--summary_freq", type=int, default=400, help="update summaries every summary_freq steps")
parser.add_argument("--progress_freq", type=int, default=200, help="display progress every progress_freq steps")
parser.add_argument("--trace_freq", type=int, default=0, help="trace execution every trace_freq steps")
parser.add_argument("--display_freq", type=int, default=0, help="write current training images every display_freq steps")
parser.add_argument("--save_freq", type=int, default=200, help="save model every save_freq steps, 0 to disable")

parser.add_argument("--batch_size", type=int, default=100, help="number of images in batch")
parser.add_argument("--which_direction", type=str, default="AtoB", choices=["AtoB", "BtoA"])
parser.add_argument("--ngf", type=int, default=96, help="number of generator filters in first conv layer")
parser.add_argument("--ndf", type=int, default=96, help="number of discriminator filters in first conv layer")
parser.add_argument("--lr", type=float, default=0.002, help="initial learning rate for adam")
parser.add_argument("--beta1", type=float, default=0.9, help="momentum term of adam")
parser.add_argument("--l1_weight", type=float, default=1.0, help="weight on L1 term for generator gradient")
parser.add_argument("--gan_weight", type=float, default=1.0, help="weight on GAN term for generator gradient")
parser.add_argument("--dropout", type=float, default=0.5, help="dropout rate")
parser.add_argument("--skip_layers", type=bool, default=False, help="add skip layers")
parser.add_argument("--convolution", type=bool, default=False, help="use convolution")
parser.add_argument("--lstm", type=bool, default=False, help="use LSTM")

# export options
parser.add_argument("--output_filetype", default="png", choices=["png", "jpeg"])
a = parser.parse_args()

EPS = 1e-12

Examples = collections.namedtuple("Examples", "paths, inputs, targets, count, steps_per_epoch")
Model = collections.namedtuple("Model", "outputs, predict_real, predict_fake, discrim_loss, discrim_grads_and_vars, gen_loss_GAN, gen_loss_L1, gen_grads_and_vars, train")


def preprocess(image):
    with tf.name_scope("preprocess"):
        # [0, 1] => [-1, 1]
        return image * 2 - 1


def deprocess(image):
    with tf.name_scope("deprocess"):
        # [-1, 1] => [0, 1]
        return (image + 1) / 2


def conv(batch_input, out_channels, stride, filter_size = 4):
    with tf.variable_scope("conv"):
        in_channels = batch_input.get_shape()[3]
        filter = tf.get_variable("filter", [filter_size, filter_size, in_channels, out_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        # [batch, in_height, in_width, in_channels], [filter_width, filter_height, in_channels, out_channels]
        #     => [batch, out_height, out_width, out_channels]
        conv = tf.nn.conv2d(batch_input, filter, [1, stride, stride, 1], padding="SAME")
        return conv


def deconv(batch_input, out_channels, stride = 2, filter_size = 4):
    with tf.variable_scope("deconv"):
        batch, in_height, in_width, in_channels = [int(d) for d in batch_input.get_shape()]
        filter = tf.get_variable("filter", [filter_size, filter_size, out_channels, in_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        # [batch, in_height, in_width, in_channels], [filter_width, filter_height, out_channels, in_channels]
        #     => [batch, out_height, out_width, out_channels]
        conv = tf.nn.conv2d_transpose(batch_input, filter, [batch, in_height * stride, in_width * stride, out_channels], [1, stride, stride, 1], padding="SAME")
        return conv


def highway_conv(batch_input, out_channels, stride, filter_size = 4, carry_bias=-0.8):
    with tf.variable_scope("highway_conv"):
        batch_input = tf.identity(batch_input)

        in_channels = batch_input.get_shape()[3]
        # [batch, in_height, in_width, in_channels], [filter_width, filter_height, in_channels, out_channels]
        #     => [batch, out_height, out_width, out_channels]
        W = tf.get_variable("W", [filter_size, filter_size, in_channels, out_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        W_T = tf.get_variable("W_T", [filter_size, filter_size, in_channels, out_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        b = tf.get_variable("b", [out_channels], dtype=tf.float32, initializer=tf.constant_initializer(0.1))
        b_T = tf.get_variable("b_T", [out_channels], dtype=tf.float32, initializer=tf.constant_initializer(carry_bias))
        
        conv = tf.nn.conv2d(batch_input, W, [1, stride, stride, 1], padding="SAME")
        conv_T = tf.nn.conv2d(batch_input, W_T, [1, stride, stride, 1], padding="SAME")
        
        H = tf.nn.relu(conv + b, name='activation')
        T = tf.sigmoid(conv_T + b_T, name='transform_gate')
        C = tf.subtract(1.0, T, name="carry_gate")
        return tf.add(tf.multiply(H, T), tf.multiply(conv_T, C), 'batch_output') # y = (H * T) + (x * C)


def highway_deconv(batch_input, out_channels, stride = 2, filter_size = 4, carry_bias=-0.8):
    with tf.variable_scope("highway_deconv"):
        # [batch, in_height, in_width, in_channels], [filter_width, filter_height, out_channels, in_channels]
        #     => [batch, out_height, out_width, out_channels]
        batch, in_height, in_width, in_channels = [int(d) for d in batch_input.get_shape()]
        W = tf.get_variable("W", [filter_size, filter_size, out_channels, in_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        W_T = tf.get_variable("W_T", [filter_size, filter_size, out_channels, in_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        b = tf.get_variable("b", [out_channels], dtype=tf.float32, initializer=tf.constant_initializer(0.1))
        b_T = tf.get_variable("b_T", [out_channels], dtype=tf.float32, initializer=tf.constant_initializer(carry_bias))

        deconv = tf.nn.conv2d_transpose(batch_input, W, [batch, in_height * stride, in_width * stride, out_channels], [1, stride, stride, 1], padding="SAME")
        deconv_T = tf.nn.conv2d_transpose(batch_input, W_T, [batch, in_height * stride, in_width * stride, out_channels], [1, stride, stride, 1], padding="SAME")
        
        H = tf.nn.relu(deconv + b, name='activation')
        T = tf.sigmoid(deconv_T + b_T, name='transform_gate')
        C = tf.subtract(1.0, T, name="carry_gate")
        return tf.add(tf.multiply(H, T), tf.multiply(deconv_T, C), 'batch_output') # y = (H * T) + (x * C)



def highway(batch_input, carry_bias=-1.0):
    with tf.variable_scope("highway"):
        in_channels = batch_input.get_shape()[3]
        out_channels = in_channels 
        W = tf.get_variable("W", [in_channels, out_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        W_T = tf.get_variable("W_T", [in_channels, out_channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(0, 0.2))
        b = tf.get_variable("b", [out_channels], dtype=tf.float32, initializer=tf.constant_initializer(0.01))
        b_T = tf.get_variable("b_T", [out_channels], dtype=tf.float32, initializer=tf.constant_initializer(carry_bias))
        
        T = tf.sigmoid(tf.matmul(batch_input, W_T) + b_T, name="transform_gate")
        H = tf.nn.relu(tf.matmul(batch_input, W) + b, name="activation")
        C = tf.subtract(1.0, T, name="carry_gate")
        
        batch_output = tf.add(tf.multiply(H, T), tf.multiply(batch_input, C), "y")  # y = (H * T) + (x * C)
        return batch_output

def lrelu(x, a):
    with tf.name_scope("lrelu"):
        # adding these together creates the leak part and linear part
        # then cancels them out by subtracting/adding an absolute value term
        # leak: a*x/2 - a*abs(x)/2
        # linear: x/2 + abs(x)/2

        # this block looks like it has 2 inputs on the graph unless we do this
        x = tf.identity(x)
        return (0.5 * (1 + a)) * x + (0.5 * (1 - a)) * tf.abs(x)


def batchnorm(input):
    with tf.variable_scope("batchnorm"):
        # this block looks like it has 3 inputs on the graph unless we do this
        input = tf.identity(input)

        channels = input.get_shape()[3]
        offset = tf.get_variable("offset", [channels], dtype=tf.float32, initializer=tf.zeros_initializer())
        scale = tf.get_variable("scale", [channels], dtype=tf.float32, initializer=tf.truncated_normal_initializer(1.0, 0.02))
        mean, variance = tf.nn.moments(input, axes=[0, 1, 2], keep_dims=False)
        variance_epsilon = 1e-5
        normalized = tf.nn.batch_normalization(input, mean, variance, offset, scale, variance_epsilon=variance_epsilon)
        return normalized





def check_image(image):
    assertion = tf.assert_equal(tf.shape(image)[-1], 1, message="image must have 1 color channels")
    with tf.control_dependencies([assertion]):
        image = tf.identity(image)

    if image.get_shape().ndims != 3:
        raise ValueError("image must be  3 dimensions")

    # make the last dimension 1 so that you can unstack the colors
    shape = list(image.get_shape())
    shape[-1] = 1
    image.set_shape(shape)
    return image



def load_examples():
    if a.input_dir is None or not os.path.exists(a.input_dir):
        raise Exception("input_dir does not exist")

    input_paths = glob.glob(os.path.join(a.input_dir, "*.jpg"))
    decode = tf.image.decode_jpeg
    if len(input_paths) == 0:
        input_paths = glob.glob(os.path.join(a.input_dir, "*.png"))
        decode = tf.image.decode_png

    if len(input_paths) == 0:
        raise Exception("input_dir contains no image files")
    
    if a.max_examples and len(input_paths) > a.max_examples:        
        input_paths = input_paths[:a.max_examples]
        

    
    def get_name(path):
        name, _ = os.path.splitext(os.path.basename(path))
        return name

    # if the image names are numbers, sort by the value rather than asciibetically
    # having sorted inputs means that the outputs are sorted in test mode
    if all(get_name(path).isdigit() for path in input_paths):
        input_paths = sorted(input_paths, key=lambda path: int(get_name(path)))
    else:
        input_paths = sorted(input_paths)

    with tf.name_scope("load_images"):
        path_queue = tf.train.string_input_producer(input_paths, shuffle=a.mode == "train")
        reader = tf.WholeFileReader()
        paths, contents = reader.read(path_queue)
        raw_input = tf.squeeze(decode(contents, channels = 1, dtype=tf.uint8))
        raw_input.set_shape([64, 512])
        raw_input = tf.image.convert_image_dtype(raw_input, dtype=tf.float32)
        # raw_input = tf.image.crop_to_bounding_box(raw_input, [200, 1280, 1]) 

        assertion1 = tf.assert_equal(tf.shape(raw_input)[0], 64, message="image does not have heigth 64")
        assertion2 = tf.assert_equal(tf.shape(raw_input)[1], 512, message="image does not have width 512")
        with tf.control_dependencies([assertion1, assertion2]):
            raw_input = tf.identity(raw_input)

        

        # break apart image pair and move to range [-1, 1]
        width = tf.shape(raw_input)[1] # [height, width]
        a_images = preprocess(raw_input[:,:256])
        b_images = preprocess(raw_input[:,256:])


    if a.which_direction == "AtoB":
        input_images, target_images = [a_images, b_images]
    elif a.which_direction == "BtoA":
        input_images, target_images = [b_images, a_images]
    else:
        raise Exception("invalid direction")

    paths_batch, inputs_batch, targets_batch = tf.train.batch([paths, input_images, target_images], batch_size=a.batch_size)
    steps_per_epoch = int(math.ceil(len(input_paths) / a.batch_size))

    return Examples(
        paths=paths_batch,
        inputs=inputs_batch,
        targets=targets_batch,
        count=len(input_paths),
        steps_per_epoch=steps_per_epoch,
    )


def create_generator(generator_inputs):
    layers = []

    if a.convolution:
        # encoder_1: [batch, 256, 256, in_channels] => [batch, 32, 32, ngf * 4]
        with tf.variable_scope("encoder_1"):
            output = conv(generator_inputs, a.ngf * 4, stride=8, filter_size=8)
            layers.append(output)

        layer_specs = [
             a.ngf * 4, # encoder_4: [batch, 32, 32, ngf * 4] => [batch, 16, 16, ngf * 4]
             a.ngf * 4, # encoder_5: [batch, 16, 16, ngf * 4] => [batch, 8, 8, ngf * 8]
        #    a.ngf * 8, # encoder_6: [batch, 8, 8, ngf * 8] => [batch, 4, 4, ngf * 8]
        #    a.ngf * 8, # encoder_7: [batch, 4, 4, ngf * 8] => [batch, 2, 2, ngf * 8]
        #    a.ngf * 8, # encoder_8: [batch, 2, 2, ngf * 8] => [batch, 1, 1, ngf * 8]
        ]
    
        for out_channels in layer_specs:
            with tf.variable_scope("encoder_%d" % (len(layers) + 1)):
                output = lrelu(output, 0.2)
                # [batch, in_height, in_width, in_channels] => [batch, in_height/2, in_width/2, out_channels]
                output = conv(output, out_channels, stride=2)
                output = batchnorm(output)
                layers.append(output)
                
        # add 16 highway layers
        for level in range(4):
            with tf.variable_scope("generator_rnn_%d" % level):#, reuse = (level > 0)) as scope:
                out_channels = a.ngf * 4
                output = lrelu(output, 0.2)
                # [batch, in_height, in_width, in_channels] => [batch, in_height/2, in_width/2, out_channels]
                output = conv(output, out_channels, stride=1)
                output = batchnorm(output)
                output = tf.nn.dropout(output, keep_prob=1 - a.dropout)
                layers.append(output)            
    
    
        #for out_channels in layer_specs:
        #    with tf.variable_scope("encoder_%d" % (len(layers) + 1)):
        #        outout = conv(output, out_channels, stride=2)
        #        layers.append(output)
    
        layer_specs = [
        #    (a.ngf * 8, a.dropout),   # decoder_8: [batch, 1, 1, ngf * 8] => [batch, 2, 2, ngf * 8 * 2]
        #    (a.ngf * 8, a.dropout),   # decoder_7: [batch, 2, 2, ngf * 8 * 2] => [batch, 4, 4, ngf * 8 * 2]
        #    (a.ngf * 8, a.dropout),   # decoder_6: [batch, 4, 4, ngf * 8 * 2] => [batch, 8, 8, ngf * 8 * 2]
             (a.ngf * 4, 0.0),   # decoder_5: [batch, 8, 8, ngf * 8 * 2] => [batch, 16, 16, ngf * 8 * 2]
             (a.ngf * 4, 0.0),   # decoder_4: [batch, 16, 16, ngf * 8 * 2] => [batch, 32, 32, ngf * 4 * 2]
        ]
    
        num_encoder_layers = len(layers)
        for decoder_layer, (out_channels, dropout) in enumerate(layer_specs):
            with tf.variable_scope("decoder_%d" % (len(layer_specs) + 1 - decoder_layer)):
                output = tf.nn.relu(output)
                # [batch, in_height, in_width, in_channels] => [batch, in_height*2, in_width*2, out_channels]
                output = deconv(output, out_channels)
                output = batchnorm(output)
    
                if dropout > 0.0:
                    output = tf.nn.dropout(output, keep_prob=1 - dropout)
    
                layers.append(output)
                
        # decoder_1: [batch, 128, 128, ngf * 2] => [batch, 256, 256, generator_outputs_channels]
        with tf.variable_scope("decoder_1"):
            output = tf.nn.relu(output)
            output = deconv(output, out_channels = 1, stride=8, filter_size=8)
            output = tf.tanh(output)
            layers.append(output)
                
    elif a.lstm:
        with tf.variable_scope("generator_lstm"):
            output = generator_inputs
            
            with tf.variable_scope("encoder"):
                cell = tf.contrib.rnn.LSTMBlockCell(2048)
                output, final_state = tf.nn.dynamic_rnn(cell, output, dtype=tf.float32, 
                                                        sequence_length=tf.constant(128, shape=(a.batch_size,)))
                
            with tf.variable_scope("decoder"):
                cell = tf.contrib.rnn.LSTMBlockCell(2048)
                output, final_state = tf.nn.dynamic_rnn(cell, output, dtype=tf.float32, initial_state=final_state,
                                                        sequence_length=tf.constant(128, shape=(a.batch_size,))) 
                output = output[:,:,::8]

    return output


def create_model(inputs, targets):
    
    with tf.variable_scope("generator"):
        outputs = create_generator(inputs)

    
    if a.gan_weight: 
        def create_discriminator(discrim_inputs, discrim_targets):
                            
            if a.convolution:
                layers = []
    
                # 2x [batch, height, width, in_channels] => [batch, height, width, in_channels * 2]
                input = tf.concat([discrim_inputs, discrim_targets], axis=3)
                
                # layer_1: [batch, 256, 256, in_channels * 2] => [batch, 128, 128, ndf]
                with tf.variable_scope("layer_1"):
                    output = conv(input, a.ndf * 4, stride=8, filter_size=8)
                    output = lrelu(output, 0.2)
                    layers.append(output)
                    
                # layer_4: [batch, 32, 32, ndf * 4] => [batch, 16, 16, ndf * 4]
                # layer_5: [batch, 16, 16, ndf * 4] => [batch, 8, 8, ndf * 4]
                for i in range(2):
                    with tf.variable_scope("layer_%d" % (len(layers) + 1)):
                        out_channels = a.ndf * 4 #a.ndf * min(2**(i+1), 8)
                        output = conv(output, out_channels, stride=2)
                        output = batchnorm(output)
                        output = lrelu(output, 0.2)
                        layers.append(output)
    
                n_layers = 4
                # layer_4: [batch, 8, 8, ndf * 4] => [batch, 8, 8, ndf * 4]
                for level in range(n_layers):
                    with tf.variable_scope("discriminator_rnn_%d" % level):#, reuse = (i > 0)):
                        out_channels = a.ndf * 4 #a.ndf * min(2**(i+1), 8)
                        output = conv(output, out_channels, stride=1)
                        output = batchnorm(output)
                        output = lrelu(output, 0.2)
                        output = tf.nn.dropout(output, keep_prob=1 - a.dropout)
                        layers.append(output)
                        
                        
                # layer_5: [batch, 8, 8, ndf * 4] => [batch, 8, 8, 1]
                with tf.variable_scope("layer_%d" % (len(layers) + 1)):
                    output = conv(output, out_channels=1, stride=1)
                    output = tf.sigmoid(output)
                    layers.append(output)
    
                        
            elif a.lstm:
                with tf.variable_scope("discriminator_lstm"):
                    def f(output): 
                        cell = tf.contrib.rnn.LSTMBlockCell(256)
                        output, final_state = tf.nn.dynamic_rnn(cell, output, dtype=tf.float32)
                        return output
                                                
                    with tf.variable_scope("encode_inputs"):
                        encoded_inputs = f(discrim_inputs)
    
                    with tf.variable_scope("encode_targets"):
                        encoded_targets = f(discrim_targets)
                        
                        
                    #output = tf.concat([encoded_inputs, encoded_targets], axis=3)
                    output = tf.abs(encoded_inputs - encoded_targets)
                    output = tf.sigmoid(output)
    
            return output
    
        
        # create two copies of discriminator, one for real pairs and one for fake pairs
        # they share the same underlying variables
        with tf.name_scope("real_discriminator"):
            with tf.variable_scope("discriminator"):
                # 2x [batch, height, width, channels] => [batch, 30, 30, 1]
                predict_real = create_discriminator(inputs, targets)
    
        with tf.name_scope("fake_discriminator"):
            with tf.variable_scope("discriminator", reuse=True):
                # 2x [batch, height, width, channels] => [batch, 30, 30, 1]
                predict_fake = create_discriminator(inputs, outputs)
    
        with tf.name_scope("discriminator_loss"):
            # minimizing -tf.log will try to get inputs to 1
            # predict_real => 1
            # predict_fake => 0
            discrim_loss = tf.reduce_mean(-(tf.log(predict_real + EPS) + tf.log(1 - predict_fake + EPS)))
    
        with tf.name_scope("generator_loss_gan"):
            gen_loss_GAN = tf.reduce_mean(-tf.log(predict_fake + EPS))
    
                 
        with tf.name_scope("discriminator_train"):
            discrim_tvars = [var for var in tf.trainable_variables() if var.name.startswith("discriminator")]
            discrim_optim = tf.train.AdamOptimizer(a.lr, a.beta1)
            discrim_grads_and_vars = discrim_optim.compute_gradients(discrim_loss, var_list=discrim_tvars)
            discrim_grads_and_vars = [(tf.clip_by_value(grad, -0.5, 0.5), var) for grad, var in discrim_grads_and_vars]
            discrim_train = [discrim_optim.apply_gradients(discrim_grads_and_vars)]

    else:
        gen_loss_GAN = 0.0
        discrim_grads_and_vars = []
        discrim_train = []
        predict_real=[]
        predict_fake=[]
        discrim_loss=[]



    with tf.name_scope("generator_loss_L1"):
        # predict_fake => 1
        # abs(targets - outputs) => 0
        gen_loss_L1 = tf.reduce_mean(tf.abs(inputs - outputs))      # !!!!!!!! targets

    with tf.name_scope("generator_loss"):
        gen_loss = gen_loss_L1 * a.l1_weight + gen_loss_GAN * a.gan_weight

    
    with tf.name_scope("generator_train"):
        with tf.control_dependencies(discrim_train):
            gen_tvars = [var for var in tf.trainable_variables() if var.name.startswith("generator")]
            gen_optim = tf.train.AdamOptimizer(a.lr, a.beta1)
            #gen_optim = tf.train.RMSPropOptimizer(a.lr)
            gen_grads_and_vars = gen_optim.compute_gradients(gen_loss, var_list=gen_tvars)            
            gen_grads_and_vars = [(tf.clip_by_value(grad, -0.5, 0.5), var) for grad, var in gen_grads_and_vars]
            gen_train = gen_optim.apply_gradients(gen_grads_and_vars)
            

        
    #ema = tf.train.ExponentialMovingAverage(decay=0.9)
    #update_losses = ema.apply([discrim_loss, gen_loss_GAN, gen_loss_L1])

    global_step = tf.contrib.framework.get_or_create_global_step()
    incr_global_step = tf.assign(global_step, global_step+1)

    return Model(
        predict_real=predict_real,
        predict_fake=predict_fake,
        discrim_loss=discrim_loss,
        discrim_grads_and_vars=discrim_grads_and_vars,
        gen_loss_GAN=gen_loss_GAN,
        gen_loss_L1=gen_loss_L1,
        gen_grads_and_vars=gen_grads_and_vars,
        outputs=outputs,
        train=tf.group(incr_global_step, gen_train),
    )


def save_images(fetches, step=None):
    image_dir = os.path.join(a.output_dir, "images")
    if not os.path.exists(image_dir):
        os.makedirs(image_dir)

    filesets = []
    for i, in_path in enumerate(fetches["paths"]):
        name, _ = os.path.splitext(os.path.basename(in_path.decode("utf8")))
        fileset = {"name": name, "step": step}
        for kind in ["inputs", "outputs", "targets"]:
            filename = name + "-" + kind + ".png"
            if step is not None:
                filename = "%08d-%s" % (step, filename)
            fileset[kind] = filename
            out_path = os.path.join(image_dir, filename)
            contents = fetches[kind][i]
            with open(out_path, "wb") as f:
                f.write(contents)
        filesets.append(fileset)
    return filesets


def append_index(filesets, step=False):
    index_path = os.path.join(a.output_dir, "index.html")
    if os.path.exists(index_path):
        index = open(index_path, "a")
    else:
        index = open(index_path, "w")
        index.write("<html><body><table><tr>")
        if step:
            index.write("<th>step</th>")
        index.write("<th>name</th><th>input</th><th>output</th><th>target</th></tr>")

    for fileset in filesets:
        index.write("<tr>")

        if step:
            index.write("<td>%d</td>" % fileset["step"])
        index.write("<td>%s</td>" % fileset["name"])

        for kind in ["inputs", "outputs", "targets"]:
            index.write("<td><img src='images/%s'></td>" % fileset[kind])

        index.write("</tr>")
    return index_path


def main():
    if tf.__version__.split('.')[0] != "1":
        raise Exception("Tensorflow version 1 required")

    if a.seed is None:
        a.seed = random.randint(0, 2**31 - 1)

    tf.set_random_seed(a.seed)
    np.random.seed(a.seed)
    random.seed(a.seed)

    if not os.path.exists(a.output_dir):
        os.makedirs(a.output_dir)

    if a.mode == "test" or a.mode == "export":
        if a.checkpoint is None:
            raise Exception("checkpoint required for test mode")

        # load some options from the checkpoint
        options = {"which_direction", "ngf", "ndf"}
        with open(os.path.join(a.checkpoint, "options.json")) as f:
            for key, val in json.loads(f.read()).items():
                if key in options:
                    print("loaded", key, "=", val)
                    setattr(a, key, val)

    for k, v in a._get_kwargs():
        print(k, "=", v)

    with open(os.path.join(a.output_dir, "options.json"), "w") as f:
        f.write(json.dumps(vars(a), sort_keys=True, indent=4))

    if a.mode == "export":
        # export the generator to a meta graph that can be imported later for standalone generation
        CROP_SIZE = 256
        input = tf.placeholder(tf.string, shape=[1])
        input_data = tf.decode_base64(input[0])
        input_image = tf.image.decode_png(input_data)
        input_image = tf.image.convert_image_dtype(input_image, dtype=tf.float32)
        input_image.set_shape([CROP_SIZE, CROP_SIZE, 3])
        batch_input = tf.expand_dims(input_image, axis=0)

        with tf.variable_scope("generator"):
            batch_output = deprocess(create_generator(preprocess(batch_input), 3))

        output_image = tf.image.convert_image_dtype(batch_output, dtype=tf.uint8)[0]
        if a.output_filetype == "png":
            output_data = tf.image.encode_png(output_image)
        elif a.output_filetype == "jpeg":
            output_data = tf.image.encode_jpeg(output_image, quality=100)
        else:
            raise Exception("invalid filetype")
        output = tf.convert_to_tensor([tf.encode_base64(output_data)])

        key = tf.placeholder(tf.string, shape=[1])
        inputs = {
            "key": key.name,
            "input": input.name
        }
        tf.add_to_collection("inputs", json.dumps(inputs))
        outputs = {
            "key":  tf.identity(key).name,
            "output": output.name,
        }
        tf.add_to_collection("outputs", json.dumps(outputs))

        init_op = tf.global_variables_initializer()
        restore_saver = tf.train.Saver()
        export_saver = tf.train.Saver()

        with tf.Session() as sess:
            sess.run(init_op)
            print("loading model from checkpoint")
            checkpoint = tf.train.latest_checkpoint(a.checkpoint)
            restore_saver.restore(sess, checkpoint)
            print("exporting model")
            export_saver.export_meta_graph(filename=os.path.join(a.output_dir, "export.meta"))
            export_saver.save(sess, os.path.join(a.output_dir, "export"), write_meta_graph=False)

        return

    examples = load_examples()
    print("examples count = %d" % examples.count)
        

    # inputs and targets are [batch_size, height, width, channels]
    model = create_model(examples.inputs, examples.targets)
    inputs = deprocess(examples.inputs)
    targets = deprocess(examples.targets)
    outputs = deprocess(model.outputs)

    def convert(image, saturate = True):
        return tf.image.convert_image_dtype(tf.expand_dims(image, -1), dtype=tf.uint8, saturate=saturate)

    # reverse any processing on images so they can be written to disk or displayed to user
    with tf.name_scope("convert_inputs"):
        converted_inputs = convert(inputs)

    with tf.name_scope("convert_targets"):
        converted_targets = convert(targets)

    with tf.name_scope("convert_outputs"):
        converted_outputs = convert(outputs)

    with tf.name_scope("encode_images"):
        display_fetches = {
            "paths": examples.paths,
            "inputs": tf.map_fn(tf.image.encode_png, converted_inputs, dtype=tf.string, name="input_pngs"),
            "targets": tf.map_fn(tf.image.encode_png, converted_targets, dtype=tf.string, name="target_pngs"),
            "outputs": tf.map_fn(tf.image.encode_png, converted_outputs, dtype=tf.string, name="output_pngs"),
        }

    # summaries
    with tf.name_scope("inputs_summary"):
        tf.summary.image("inputs", converted_inputs)

    with tf.name_scope("targets_summary"):
        tf.summary.image("targets", converted_targets)

    with tf.name_scope("outputs_summary"):
        tf.summary.image("outputs", converted_outputs)

    #with tf.name_scope("predict_real_summary"):
    #    tf.summary.image("predict_real", convert(model.predict_real, saturate=False))

    #with tf.name_scope("predict_fake_summary"):
    #    tf.summary.image("predict_fake", convert(model.predict_fake, saturate=False))

    #tf.summary.scalar("discriminator_loss", model.discrim_loss)
    #tf.summary.scalar("generator_loss_GAN", model.gen_loss_GAN)
    tf.summary.scalar("generator_loss_L1", model.gen_loss_L1)

    for var in tf.trainable_variables():
        tf.summary.histogram(var.op.name + "/values", var)

    for grad, var in model.discrim_grads_and_vars + model.gen_grads_and_vars:
        tf.summary.histogram(var.op.name + "/gradients", grad)

    with tf.name_scope("parameter_count"):
        parameter_count = tf.reduce_sum([tf.reduce_prod(tf.shape(v)) for v in tf.trainable_variables()])

    saver = tf.train.Saver(max_to_keep=1)

    logdir = a.output_dir if (a.trace_freq > 0 or a.summary_freq > 0) else None
    sv = tf.train.Supervisor(logdir=logdir, save_summaries_secs=0, saver=None)
    with sv.managed_session() as sess:
        print("parameter_count =", sess.run(parameter_count))

        if a.checkpoint is not None:
            print("loading model from checkpoint")
            checkpoint = tf.train.latest_checkpoint(a.checkpoint)
            saver.restore(sess, checkpoint)

        max_steps = 2**32
        if a.max_epochs is not None:
            max_steps = examples.steps_per_epoch * a.max_epochs
        if a.max_steps is not None:
            max_steps = a.max_steps

        if a.mode == "test":
            # testing
            # at most, process the test data once
            max_steps = min(examples.steps_per_epoch, max_steps)
            for step in tqdm(range(max_steps)):
                results = sess.run(display_fetches)
                filesets = save_images(results)
                for i, f in enumerate(filesets):
                    print("evaluated image", f["name"])
                index_path = append_index(filesets)

            print("wrote index at", index_path)
        else:
            # training
            start = time.time()

            for step in tqdm(range(max_steps)):
                def should(freq):
                    return freq > 0 and ((step + 1) % freq == 0 or step == max_steps - 1)

                options = None
                run_metadata = None
                if should(a.trace_freq):
                    options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()

                fetches = {
                    "train": model.train,
                    "global_step": sv.global_step,
                }

                if should(a.progress_freq):
                    print("fetching progress"), sys.stdout.flush()
                    #fetches["discrim_loss"] = model.discrim_loss
                    #fetches["gen_loss_GAN"] = model.gen_loss_GAN
                    fetches["gen_loss_L1"] = model.gen_loss_L1

                if should(a.summary_freq):
                    print("fetching summary"), sys.stdout.flush()
                    fetches["summary"] = sv.summary_op

                if should(a.display_freq):
                    print("fetching display"), sys.stdout.flush()
                    fetches["display"] = display_fetches

                results = sess.run(fetches, options=options, run_metadata=run_metadata)

                if should(a.summary_freq):
                    print("recording summary"), sys.stdout.flush()
                    sv.summary_writer.add_summary(results["summary"], results["global_step"])

                if should(a.display_freq):
                    print("saving display images"), sys.stdout.flush()
                    filesets = save_images(results["display"], step=results["global_step"])
                    append_index(filesets, step=True)

                if should(a.trace_freq):
                    print("recording trace"), sys.stdout.flush()
                    sv.summary_writer.add_run_metadata(run_metadata, "step_%d" % results["global_step"])

                if should(a.progress_freq):
                    print("printing progress trace"), sys.stdout.flush()
                    # global_step will have the correct step count if we resume from a checkpoint
                    train_epoch = math.ceil(results["global_step"] / examples.steps_per_epoch)
                    train_step = (results["global_step"] - 1) % examples.steps_per_epoch + 1
                    rate = (step + 1) * a.batch_size / (time.time() - start)
                    remaining = (max_steps - step) * a.batch_size / rate
                    print("progress  epoch %d  step %d  image/sec %0.1f  remaining %dm" % (train_epoch, train_step, rate, remaining / 60))
                    #print("discrim_loss", results["discrim_loss"])
                    #print("gen_loss_GAN", results["gen_loss_GAN"])
                    print("gen_loss_L1", results["gen_loss_L1"])

                if should(a.save_freq):
                    print("saving model")
                    saver.save(sess, os.path.join(a.output_dir, "model"), global_step=sv.global_step)

                if sv.should_stop():
                    print("terminating"), sys.stdout.flush()
                    break


main()
