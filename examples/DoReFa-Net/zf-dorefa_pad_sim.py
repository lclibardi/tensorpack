#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: alexnet-dorefa.py
# Author: Yuxin Wu, Yuheng Zou ({wyx,zyh}@megvii.com)

import cv2
import tensorflow as tf
import argparse
import numpy as np
import multiprocessing
import msgpack
import os, sys

from tensorpack import *
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *
from dorefa import get_dorefa
import data

"""
This is a tensorpack script for the ImageNet results in paper:
DoReFa-Net: Training Low Bitwidth Convolutional Neural Networks with Low Bitwidth Gradients
http://arxiv.org/abs/1606.06160

The original experiements are performed on a proprietary framework.
This is our attempt to reproduce it on tensorpack/tensorflow.

Accuracy:
    Trained with 4 GPUs and (W,A,G)=(1,2,6), it can reach top-1 single-crop validation error of 51%,
    after 70 epochs. This number is a bit better than what's in the paper
    probably due to more sophisticated augmentors.

    Note that the effective batch size in SyncMultiGPUTrainer is actually
    BATCH_SIZE * NUM_GPU. With a different number of GPUs in use, things might
    be a bit different, especially for learning rate.

    With (W,A,G)=(32,32,32), 43% error.
    With (W,A,G)=(1,2,6), 51% error.
    With (W,A,G)=(1,2,4), 63% error.

Speed:
    About 3.5 iteration/s on 4 Tesla M40. (Each epoch is set to 10000 iterations)

To Train:
    ./alexnet-dorefa.py --dorefa 1,2,6 --data PATH --gpu 0,1,2,3

    PATH should look like:
    PATH/
      train/
        n02134418/
          n02134418_198.JPEG
          ...
        ...
      val/
        ILSVRC2012_val_00000001.JPEG
        ...

    And better to have:
        Fast disk random access (Not necessarily SSD. I used a RAID of HDD, but not sure if plain HDD is enough)
        More than 12 CPU cores (for data processing)

To Run Pretrained Model:
    ./zf-dorefa_pad_sim.py --load zf_pad_64.npy --run cat.jpg --dorefa 1,4,32
"""

BITW = 1
BITA = 4
BITG = 32
BATCH_SIZE = 32

class Model(ModelDesc):
    def _get_input_vars(self):
        return [InputVar(tf.float32, [None, 224, 224, 3], 'input'),
                InputVar(tf.int32, [None], 'label') ]

    def _build_graph(self, input_vars, is_training):
        image, label = input_vars
        #image = image / 255.0
        image = image / 128.0

        fw, fa, fg = get_dorefa(BITW, BITA, BITG)
        # monkey-patch tf.get_variable to apply fw
        old_get_variable = tf.get_variable
        def new_get_variable(name, shape=None, **kwargs):
            v = old_get_variable(name, shape, **kwargs)
            # don't binarize first and last layer
            # if name != 'W' or 'conv0' in v.op.name or 'fct' in v.op.name:
            if name != 'W':
                return v
            else:
                logger.info("Binarizing weight {}".format(v.op.name))
                return fw(v)
        tf.get_variable = new_get_variable

        def nonlin(x):
            if BITA == 32:
                return tf.nn.relu(x)    # still use relu for 32bit cases
            return tf.clip_by_value(x, 0.0, 1.0)

        def activate(x):
            return fa(nonlin(x))

        with argscope(BatchNorm, decay=0.9, epsilon=1e-4, use_local_stat=is_training), \
                argscope([Conv2D, FullyConnected], use_bias=False, nl=tf.identity):
            logits = (LinearWrap(image)
                .Conv2D('conv0', 64, 7, stride=2, padding='SAME')
                .apply(fg)())
            print(logits)
            
            logits = (LinearWrap(logits)
                .BatchNorm('bn0')())
            print(logits)

            logits = (LinearWrap(logits)
                .apply(activate)())
            print(logits)

            logits = (LinearWrap(logits)
                .MaxPooling('pool0', 3, 2, padding='SAME')())
            print(logits)

            logits = (LinearWrap(logits)
                .Conv2D('conv1', 256, 5, stride=2, padding='SAME')
                .apply(fg)())
            print(logits)

            logits = (LinearWrap(logits)
                .BatchNorm('bn1')())
            print(logits)

            logits = (LinearWrap(logits)
                .apply(activate)())
            print(logits)

            logits = (LinearWrap(logits)
                .MaxPooling('pool1', 3, 2, padding='SAME')())
            print(logits)

            logits = (LinearWrap(logits)
                .Conv2D('conv2', 384, 3, stride=1, padding='SAME')
                .apply(fg)
                .BatchNorm('bn2')
                .apply(activate)())
            print(logits)

            logits = (LinearWrap(logits)
                .Conv2D('conv3', 384, 3, stride=1, padding='SAME')
                .apply(fg)
                .BatchNorm('bn3')
                .apply(activate)())            
            print(logits)

            logits = (LinearWrap(logits)
                .Conv2D('conv4', 256, 3, stride=1, padding='SAME')
                .apply(fg)
                .BatchNorm('bn4')
                .apply(activate)())
            print(logits)

            logits = (LinearWrap(logits)
                .MaxPooling('pool4', 3, 2, padding='SAME')())
            print(logits)

            logits = (LinearWrap(logits)            
                .FullyConnected('fc0', 4096)
                .apply(fg)())
            print(logits)

            logits = (LinearWrap(logits)
                .BatchNorm('bnfc0')())
            print(logits)

            logits = (LinearWrap(logits)
                .apply(activate)())
            print(logits)

            logits = (LinearWrap(logits)
                .FullyConnected('fc1', 4096)
                .apply(fg)
                .BatchNorm('bnfc1')
                .apply(activate)())
            print(logits)
            logits = (LinearWrap(logits)
                .FullyConnected('fct', 1000)())
            print(logits)           
        tf.get_variable = old_get_variable

        prob = tf.nn.softmax(logits, name='output')

        cost = tf.nn.sparse_softmax_cross_entropy_with_logits(logits, label)
        cost = tf.reduce_mean(cost, name='cross_entropy_loss')

        wrong = prediction_incorrect(logits, label, 1)
        nr_wrong = tf.reduce_sum(wrong, name='wrong-top1')
        add_moving_summary(tf.reduce_mean(wrong, name='train_error_top1'))
        wrong = prediction_incorrect(logits, label, 5)
        nr_wrong = tf.reduce_sum(wrong, name='wrong-top5')
        add_moving_summary(tf.reduce_mean(wrong, name='train_error_top5'))

        # weight decay on all W of fc layers
        wd_cost = regularize_cost('fc.*/W', l2_regularizer(5e-6))
        add_moving_summary(cost, wd_cost)

        add_param_summary([('.*/W', ['histogram', 'rms'])])
        self.cost = tf.add_n([cost, wd_cost], name='cost')

def get_data(dataset_name):
    isTrain = dataset_name == 'train'
    ds = dataset.ILSVRC12(args.data, dataset_name, shuffle=isTrain)

    meta = dataset.ILSVRCMeta()
    pp_mean = meta.get_per_pixel_mean()
    pp_mean_224 = pp_mean[16:-16,16:-16,:]

    if isTrain:
        class Resize(imgaug.ImageAugmentor):
            def __init__(self):
                self._init(locals())
            def _augment(self, img, _):
                h, w = img.shape[:2]
                size = 224
                scale = self.rng.randint(size, 308) * 1.0 / min(h, w)
                scaleX = scale * self.rng.uniform(0.85, 1.15)
                scaleY = scale * self.rng.uniform(0.85, 1.15)
                desSize = map(int, (max(size, min(w, scaleX * w)),\
                    max(size, min(h, scaleY * h))))
                dst = cv2.resize(img, tuple(desSize),
                     interpolation=cv2.INTER_CUBIC)
                return dst

        augmentors = [
            Resize(),
            imgaug.Rotation(max_deg=10),
            imgaug.RandomApplyAug(imgaug.GaussianBlur(3), 0.5),
            imgaug.Brightness(30, True),
            imgaug.Gamma(),
            imgaug.Contrast((0.8,1.2), True),
            imgaug.RandomCrop((224, 224)),
            imgaug.RandomApplyAug(imgaug.JpegNoise(), 0.8),
            imgaug.RandomApplyAug(imgaug.GaussianDeform(
                [(0.2, 0.2), (0.2, 0.8), (0.8,0.8), (0.8,0.2)],
                (224, 224), 0.2, 3), 0.1),
            imgaug.Flip(horiz=True),
            imgaug.MapImage(lambda x: x - 128),
        ]
    else:
        def resize_func(im):
            h, w = im.shape[:2]
            scale = 256.0 / min(h, w)
            desSize = map(int, (max(224, min(w, scale * w)),\
                                max(224, min(h, scale * h))))
            im = cv2.resize(im, tuple(desSize), interpolation=cv2.INTER_CUBIC)
            return im
        augmentors = [
            imgaug.MapImage(resize_func),
            imgaug.CenterCrop((224, 224)),
            imgaug.MapImage(lambda x: x - pp_mean_224),
        ]
    ds = AugmentImageComponent(ds, augmentors)
    ds = BatchData(ds, BATCH_SIZE, remainder=not isTrain)
    if isTrain:
        ds = PrefetchDataZMQ(ds, min(12, multiprocessing.cpu_count()))
    return ds

def get_config():
    logger.auto_set_dir()

    # prepare dataset
    data_train = get_data('train')
    data_test = get_data('val')

    lr = tf.Variable(1e-4, trainable=False, name='learning_rate')
    tf.scalar_summary('learning_rate', lr)

    return TrainConfig(
        dataset=data_train,
        optimizer=tf.train.AdamOptimizer(lr, epsilon=1e-5),
        callbacks=Callbacks([
            StatPrinter(),
            ModelSaver(),
            #HumanHyperParamSetter('learning_rate'),
            ScheduledHyperParamSetter(
                'learning_rate', [(56, 2e-5), (64, 4e-6)]),
            InferenceRunner(data_test,
                [ScalarStats('cost'),
                 ClassificationError('wrong-top1', 'val-top1-error'),
                 ClassificationError('wrong-top5', 'val-top5-error')])
        ]),
        model=Model(),
        step_per_epoch=10000,
        max_epoch=100,
    )

def run_image(model, sess_init, inputs):
    pred_config = PredictConfig(
        model=model,
        session_init=sess_init,
        session_config=get_default_sess_config(0.9),
        input_var_names=['input'],
        output_var_names=['output', 'conv0/output:0', 'bn0/bn/add_1:0', 'div_1:0', 'pool0/MaxPool:0', 'conv1/output:0', 'bn1/bn/add_1:0', 'div_2:0', 'pool1/MaxPool:0', 'div_3:0', 'div_4:0', 'div_5:0', 'pool4/MaxPool', 'fc0/output:0', 'bnfc0/bn/add_1:0', 'div_6:0', 'div_7:0', 'fct/output:0']
    )
    predict_func = get_predict_func(pred_config)
    meta = dataset.ILSVRCMeta()
    pp_mean = meta.get_per_pixel_mean()
    pp_mean_224 = pp_mean[16:-16,16:-16,:]
    words = meta.get_synset_words_1000()

    def resize_func(im):
        h, w = im.shape[:2]
        scale = 256.0 / min(h, w)
        desSize = map(int, (max(224, min(w, scale * w)),\
                            max(224, min(h, scale * h))))
        im = cv2.resize(im, tuple(desSize), interpolation=cv2.INTER_CUBIC)
        return im
    transformers = imgaug.AugmentorList([
        imgaug.MapImage(resize_func),
        imgaug.CenterCrop((224, 224)),
        #imgaug.MapImage(lambda x: x - pp_mean_224),
        imgaug.MapImage(lambda x: x - 128)
    ])
    for f in inputs:
        assert os.path.isfile(f)
        img = cv2.imread(f).astype('float32')
        assert img is not None
        #img = np.round(img)
        #print img
        #data.mat_dump_hex('./input_sim.dat', img-128)
        img = transformers.augment(img)[np.newaxis, :,:,:]
        img = np.round(img)
        data.mat_dump_hex('./dump/input_sim.dat', img, True)
        data.mat_dump_int('./dump/input.dat', img, True)
        outputs = predict_func([img])
        inter_layers = ['conv0', 'bn0', 'active0', 'pool0', 'conv1', 'bn1', 'active1', 'pool1', 'conv2', 'conv3', 'conv4', 'pool4', 'fc0', 'fc0bn', 'fc0active', 'fc1', 'fct']

        #print outputs[1]
        for i, name in enumerate(inter_layers):
            data.mat_dump_float('dump/' + name + '.dat', outputs[i+1])
        prob = outputs[0][0]
        ret = prob.argsort()[-10:][::-1]

        names = [words[i] for i in ret]
        print(f + ":")
        print(list(zip(names, prob[ret])))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='the physical ids of GPUs to use')
    parser.add_argument('--load', help='load a checkpoint, or a npy (given as the pretrained model)')
    parser.add_argument('--data', help='ILSVRC dataset dir')
    parser.add_argument('--dorefa',
            help='number of bits for W,A,G, separated by comma. Defaults to \'1,2,4\'',
            default='1,2,4')
    parser.add_argument('--run', help='run on a list of images with the pretrained model', nargs='*')
    args = parser.parse_args()

    BITW, BITA, BITG = map(int, args.dorefa.split(','))

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.run:
        assert args.load.endswith('.npy')
        run_image(Model(), ParamRestore(np.load(args.load, encoding='latin1').item()), args.run)
        sys.exit()

    config = get_config()
    if args.load:
        config.session_init = SaverRestore(args.load)
    if args.gpu:
        config.nr_tower = len(args.gpu.split(','))
    SyncMultiGPUTrainer(config).train()
