#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

import unittest
import paddle.v2.fluid as fluid
import paddle.v2 as paddle
import contextlib
import math
import numpy as np
import sys


def convolution_net(data, label, input_dim, class_dim=2, emb_dim=32,
                    hid_dim=32):
    emb = fluid.layers.embedding(input=data, size=[input_dim, emb_dim])
    conv_3 = fluid.nets.sequence_conv_pool(
        input=emb,
        num_filters=hid_dim,
        filter_size=3,
        act="tanh",
        pool_type="sqrt")
    conv_4 = fluid.nets.sequence_conv_pool(
        input=emb,
        num_filters=hid_dim,
        filter_size=4,
        act="tanh",
        pool_type="sqrt")
    prediction = fluid.layers.fc(input=[conv_3, conv_4],
                                 size=class_dim,
                                 act="softmax")
    cost = fluid.layers.cross_entropy(input=prediction, label=label)
    avg_cost = fluid.layers.mean(x=cost)
    adam_optimizer = fluid.optimizer.Adam(learning_rate=0.002)
    adam_optimizer.minimize(avg_cost)
    accuracy = fluid.layers.accuracy(input=prediction, label=label)
    return avg_cost, accuracy, prediction


def stacked_lstm_net(data,
                     label,
                     input_dim,
                     class_dim=2,
                     emb_dim=128,
                     hid_dim=512,
                     stacked_num=3):
    assert stacked_num % 2 == 1

    emb = fluid.layers.embedding(input=data, size=[input_dim, emb_dim])
    # add bias attr

    # TODO(qijun) linear act
    fc1 = fluid.layers.fc(input=emb, size=hid_dim)
    lstm1, cell1 = fluid.layers.dynamic_lstm(input=fc1, size=hid_dim)

    inputs = [fc1, lstm1]

    for i in range(2, stacked_num + 1):
        fc = fluid.layers.fc(input=inputs, size=hid_dim)
        lstm, cell = fluid.layers.dynamic_lstm(
            input=fc, size=hid_dim, is_reverse=(i % 2) == 0)
        inputs = [fc, lstm]

    fc_last = fluid.layers.sequence_pool(input=inputs[0], pool_type='max')
    lstm_last = fluid.layers.sequence_pool(input=inputs[1], pool_type='max')

    prediction = fluid.layers.fc(input=[fc_last, lstm_last],
                                 size=class_dim,
                                 act='softmax')
    cost = fluid.layers.cross_entropy(input=prediction, label=label)
    avg_cost = fluid.layers.mean(x=cost)
    adam_optimizer = fluid.optimizer.Adam(learning_rate=0.002)
    adam_optimizer.minimize(avg_cost)
    accuracy = fluid.layers.accuracy(input=prediction, label=label)
    return avg_cost, accuracy, prediction


def create_random_lodtensor(lod, place, low, high):
    data = np.random.random_integers(low, high, [lod[-1], 1]).astype("int64")
    res = fluid.LoDTensor()
    res.set(data, place)
    res.set_lod([lod])
    return res


def train(word_dict, nn_type, use_cuda, save_dirname=None):
    BATCH_SIZE = 128
    PASS_NUM = 5
    dict_dim = len(word_dict)
    class_dim = 2

    data = fluid.layers.data(
        name="words", shape=[1], dtype="int64", lod_level=1)
    label = fluid.layers.data(name="label", shape=[1], dtype="int64")

    if nn_type == "conv":
        net_method = convolution_net
    else:
        net_method = stacked_lstm_net
    cost, acc_out, prediction = net_method(
        data, label, input_dim=dict_dim, class_dim=class_dim)

    train_data = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.imdb.train(word_dict), buf_size=1000),
        batch_size=BATCH_SIZE)
    place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
    exe = fluid.Executor(place)
    feeder = fluid.DataFeeder(feed_list=[data, label], place=place)

    exe.run(fluid.default_startup_program())

    for pass_id in xrange(PASS_NUM):
        for data in train_data():
            cost_val, acc_val = exe.run(fluid.default_main_program(),
                                        feed=feeder.feed(data),
                                        fetch_list=[cost, acc_out])
            print("cost=" + str(cost_val) + " acc=" + str(acc_val))
            if cost_val < 0.4 and acc_val > 0.8:
                if save_dirname is not None:
                    fluid.io.save_inference_model(save_dirname, ["words"],
                                                  prediction, exe)
                return
            if math.isnan(float(cost_val)):
                sys.exit("got NaN loss, training failed.")
    raise AssertionError("Cost is too large for {0}".format(
        net_method.__name__))


def infer(word_dict, use_cuda, save_dirname=None):
    if save_dirname is None:
        return

    place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
    exe = fluid.Executor(place)

    # Use fluid.io.load_inference_model to obtain the inference program desc,
    # the feed_target_names (the names of variables that will be feeded 
    # data using feed operators), and the fetch_targets (variables that 
    # we want to obtain data from using fetch operators).
    [inference_program, feed_target_names,
     fetch_targets] = fluid.io.load_inference_model(save_dirname, exe)

    word_dict_len = len(word_dict)

    lod = [0, 4, 10]
    tensor_words = create_random_lodtensor(
        lod, place, low=0, high=word_dict_len - 1)

    # Construct feed as a dictionary of {feed_target_name: feed_target_data}
    # and results will contain a list of data corresponding to fetch_targets.
    assert feed_target_names[0] == "words"
    results = exe.run(inference_program,
                      feed={feed_target_names[0]: tensor_words},
                      fetch_list=fetch_targets,
                      return_numpy=False)
    print(results[0].lod())
    np_data = np.array(results[0])
    print("Inference Shape: ", np_data.shape)
    print("Inference results: ", np_data)


def main(word_dict, nn_type, use_cuda):
    if use_cuda and not fluid.core.is_compiled_with_cuda():
        return

    # Directory for saving the trained model
    save_dirname = "understand_sentiment_" + nn_type + ".inference.model"

    train(word_dict, nn_type, use_cuda, save_dirname)
    infer(word_dict, use_cuda, save_dirname)


class TestUnderstandSentiment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.word_dict = paddle.dataset.imdb.word_dict()

    @contextlib.contextmanager
    def new_program_scope(self):
        prog = fluid.Program()
        startup_prog = fluid.Program()
        scope = fluid.core.Scope()
        with fluid.scope_guard(scope):
            with fluid.program_guard(prog, startup_prog):
                yield

    def test_conv_cpu(self):
        with self.new_program_scope():
            main(self.word_dict, nn_type="conv", use_cuda=False)

    def test_stacked_lstm_cpu(self):
        with self.new_program_scope():
            main(self.word_dict, nn_type="lstm", use_cuda=False)

    def test_conv_gpu(self):
        with self.new_program_scope():
            main(self.word_dict, nn_type="conv", use_cuda=True)

    def test_stacked_lstm_gpu(self):
        with self.new_program_scope():
            main(self.word_dict, nn_type="lstm", use_cuda=True)


if __name__ == '__main__':
    unittest.main()
