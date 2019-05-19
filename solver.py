from model import get_generator, get_discriminator
import tensorflow as tf
import keras
from keras.models import Model
from keras.layers import Input
import numpy as np
import os
import time
import random
import datetime
import matplotlib.pyplot as plt
from io import BytesIO
from tqdm import trange

class Solver(object):

    def __init__(self, celeba_loader, config):
        self.data_loader = celeba_loader
        self.n_labels = config.c_dim
        self.image_size = config.image_size
        self.g_conv_dim = config.g_conv_dim
        self.d_conv_dim = config.g_conv_dim
        self.g_repeat_num = config.g_repeat_num
        self.d_repeat_num = config.d_repeat_num
        self.lambda_cls = config.lambda_cls
        self.lamda_rec = config.lambda_rec
        self.lamda_gp = config.lambda_gp

        self.g_lr = config.g_lr
        self.d_lr = config.d_lr
        self.beta_1 = config.beta1
        self.beta_2 = config.beta2 

        self.batch_size = config.batch_size
        self.num_iters = config.num_iters
        self.num_iters_decay = config.num_iters_decay
        self.n_critic = config.n_critic
        self.resume_iters = config.resume_iters
        self.selected_attrs = config.selected_attrs

        self.test_iters = config.test_iters
        self.use_tensorboard = config.use_tensorboard

        self.log_dir = "stargan/logs/"
        self.sample_dir = "stargan/samples/"
        self.model_save_dir = "stargan/models/"
        self.result_dir = "stargan/results/"

        self.log_step = config.log_step
        self.sample_step = config.sample_step
        self.model_save_step = config.model_save_step
        self.lr_update_step = config.lr_update_step

        self.build_model()

    def build_model(self):
        self.G = get_generator(self.g_conv_dim, self.n_labels, self.g_repeat_num, self.image_size)
        self.D = get_discriminator(self.d_conv_dim, self.n_labels, self.d_repeat_num, self.image_size)

        self.d_optimizer = keras.optimizers.Adam(lr = self.d_lr, beta_1 = self.beta_1, beta_2 = self.beta_2, decay = 1.0/self.num_iters_decay)
        self.g_optimizer = keras.optimizers.Adam(lr = self.g_lr, beta_1 = self.beta_2, beta_2 = self.beta_2, decay = 1.0/self.num_iters_decay)

        print(self.D)
        self.D.compile(loss=["binary_crossentropy", "binary_crossentropy"], loss_weights = [1, self.lambda_cls], optimizer= self.d_optimizer)

        self.D.trainable = False

        input_img = Input(shape = (self.image_size, self.image_size, 3 + self.n_labels))

        reconstr_img = self.G(input_img)
        output_D     = self.D(reconstr_img)


        self.combined = Model(inputs = [input_img], outputs = [reconstr_img] + output_D)

        self.combined.compile(loss = ["mae", "binary_crossentropy", "binary_crossentropy"], loss_weights = [self.lamda_rec, -1, self.lambda_cls], optimizer = self.g_optimizer)

    def label2onehot(self, labels, dim):
        """Convert label indices to one-hot vectors."""
        batch_size = self.batch_size
        out = np.zeros((batch_size, dim))
        out[np.arange(batch_size), labels.astype(np.int_)] = 1
        return out

    def create_labels(self, c_org, c_dim=5, dataset='CelebA', selected_attrs=None):
        """Generate target domain labels for debugging and testing."""
        # Get hair color indices.
        hair_color_indices = []
        for i, attr_name in enumerate(selected_attrs):
            if attr_name in ['Black_Hair', 'Blond_Hair', 'Brown_Hair', 'Gray_Hair']:
                hair_color_indices.append(i)

        c_trg_list = []
        for i in range(c_dim):
            c_trg = c_org.copy()
            if i in hair_color_indices:  # Set one hair color to 1 and the rest to 0.
                c_trg[:, i] = 1
                for j in hair_color_indices:
                    if j != i:
                        c_trg[:, j] = 0
            else:
                c_trg[:, i] = (c_trg[:, i] == 0)  # Reverse attribute value.

            c_trg_list.append(c_trg)
        return c_trg_list

    def denorm(self, x):
        out = (x + 1) / 2
        return np.clip(out,0, 1)


    def train(self):
        self.writer = tf.summary.FileWriter(self.log_dir)

        callbacks = [keras.callbacks.TensorBoard(log_dir = self.log_dir, write_graph = False),
                     keras.callbacks.ModelCheckpoint(self.model_save_dir + "weights.{epoch:03d}.hdf5", verbose = 1, period = 5)]

        data_iter = iter(self.data_loader)
        
        test_imgs, label_test = next(data_iter)
        test_imgs = np.tile(test_imgs, (5,1,1,1))
        c_fixed = np.asarray(self.create_labels(label_test, self.n_labels, self.data_loader, self.selected_attrs))
        c_fixed = np.concatenate(c_fixed, axis = 0)
        labels_fixed = c_fixed.reshape((5 * self.batch_size, 1, 1, 5))
        test_imgs_concatted = np.concatenate((test_imgs, np.tile(labels_fixed, (1,self.image_size, self.image_size,1))), axis=3)

        for epoch in trange(0,self.num_iters//self.log_step):
            with keras.backend.get_session().as_default():

                outcome = self.G.predict(test_imgs_concatted)
                s = BytesIO()
                plt.imsave(s, self.denorm(outcome[epoch % 80].reshape((128,128,3))))
                out_sum = tf.Summary.Image(encoded_image_string = s.getvalue())

                s = BytesIO()
                plt.imsave(s, self.denorm(test_imgs[epoch % 80].reshape((128,128,3))))
                orig_sum = tf.Summary.Image(encoded_image_string = s.getvalue())
           
                
                summary = tf.Summary(value=[tf.Summary.Value(tag = "in", image = orig_sum), 
                                            tf.Summary.Value(tag = "Out", image = out_sum)])
                self.writer.add_summary(summary, epoch)


            d_loss_r = 0
            d_loss_f = 0
            for i in trange(0, self.log_step):

                try:
                    x_real, label_org = next(data_iter)
                except:
                    data_iter = iter(data_iter)
                    x_real, label_org = next(data_iter)

                label_trg = label_org[range(label_org.shape[0])[::-1]]

                c_org = label_org.copy()
                c_trg = label_trg.copy()

                labels_trg = c_trg.reshape((self.batch_size,1,1,5))
                x_concatted = np.concatenate((x_real, np.tile(labels_trg, (1,self.image_size, self.image_size,1))), axis=3)


                x_fake = self.G.predict(x_concatted)


                fake = np.zeros(self.batch_size)
                real = np.ones(self.batch_size)

                d_loss_r = self.D.train_on_batch(x_real, [real, c_org])
                d_loss_f = self.D.train_on_batch(x_fake, [fake, c_trg])
                g_loss = self.combined.train_on_batch(x_concatted, [x_real, fake, c_trg])

            


