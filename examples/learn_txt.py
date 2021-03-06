"""Train an LSTM on text file."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import math
import os.path
import urllib2

import numpy as np
import tensorflow as tf

_TXT_URL = "http://www.gutenberg.org/cache/epub/10/pg10.txt"
_LOG_DIR = "/tmp/train_log"

_N_STATES = 1024 + 256

def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
            "source", metavar="URL", type=str,
            help="Input text location, e.g., %s" % _TXT_URL,
            default=_TXT_URL)
    return parser

FLAGS = get_parser().parse_args()

def get_txt():
    source = FLAGS.source
    if source.startswith("http"):
        print("Downloading file from " + source)
        text = urllib2.urlopen(_TXT_URL).read()
        return text
    else:
        print("Using local file: " + source)
        return open(source, 'r').read()

def get_training_data():
    return np.array([ord(c) for c in get_txt()])

def create_lstm():
    return tf.contrib.rnn.DropoutWrapper(
        tf.contrib.rnn.BasicLSTMCell(_N_STATES),
        state_keep_prob=0.95)

def get_state_var(lstm, name):
    zero = tf.convert_to_tensor(
        lstm.zero_state(
            1, dtype=tf.float32))
    return tf.get_variable(name, zero.get_shape(), dtype=tf.float32)

def tensor_to_state_tuple(tensor):
    if len(tensor.get_shape()) <= 2:
        return tensor # Innermost tensors are preserverd
    # Outer tensor converted to tuples.
    return tuple(
        tensor_to_state_tuple(tensor[i])
        for i in xrange(tensor.get_shape()[0]))

_training = tf.get_variable("training", tuple(), tf.bool)
_set_training = tf.assign(_training, True)
_unset_training = tf.assign(_training, False)

def is_training(): return _training

def set_training_op():
    return _set_training

def unset_training_op():
    return _unset_training

_nets_created = set()

def batch_dropout_net(batched_input, layers, dropout_rate, act, name):
    num_batches = batched_input.get_shape()[0].value
    num_elems = batched_input.get_shape()[1].value

    cols = num_elems
    for i, l in enumerate(layers):
        layer_name = "%s_%d" % (name, i)
        net_exists = layer_name in _nets_created
        _nets_created.add(layer_name)

        with tf.variable_scope("batch_dropout", reuse=net_exists):
            w = tf.get_variable("%s_w" % layer_name, (cols, l))
            b = tf.get_variable("%s_b" % layer_name, (1, l))
            batched_input = act(
                tf.add(tf.matmul(batched_input, w), b))
            if (dropout_rate > 0):
                batched_input = tf.layers.dropout(
                    batched_input, dropout_rate, is_training())
        cols = l

    return batched_input

class SequencePredictor(tf.contrib.rnn.RNNCell):

    def __init__(self, lstm, depth, scope=None, reuse=False):
        self._scope = scope or "PredictTxt"
        self._depth = depth
        self._lstm = lstm
        self._pred_w = None
        self._out_w = None

    def __call__(self, inputs, state, scope=None):
        with tf.variable_scope(self._scope):
            inputs_onehot = tf.one_hot(
                inputs, self._depth, dtype=tf.float32)

            inputs_onehot = tf.reshape(
                    inputs_onehot, (inputs.get_shape()[0].value, self._depth))
            processed_input = self._input_preprocess(inputs_onehot)
            lstm_out, lstm_state = self._lstm(
                processed_input, state, scope)
            processed_output = self._output_postprocess(
                processed_input, lstm_out)
            processed_output = tf.layers.dropout(
                lstm_out, 0.4, is_training())
            logits = self._prediction_logits(processed_output)
            return (logits, lstm_state)

    @property
    def state_size(self):
        return self._lstm.state_size

    @property
    def output_size(self):
        return self._depth
    
    def zero_state(self, batch_size, dtype=tf.float32):
        return self._lstm.zero_state(batch_size, dtype)

    def _input_preprocess(self, onehot_input):
        """Creates an NN to process a batched input for the LSTM."""
        return batch_dropout_net(
                onehot_input, [512, 512], 0.1,
                tf.nn.relu, "input_preproc")

    def _output_postprocess(self, inputs_onehot, lstm_output):
        """Transforms an lstm_output before output prediction."""
        return tf.concat([lstm_output, inputs_onehot], 1)

    def _prediction_logits(self, raw_output):
        """Returns prediction logits, for processing with softmax."""
        n_columns = raw_output.get_shape()[-1].value
        self._pred_w = self._pred_w or tf.get_variable(
            "pred_w", (n_columns, self.output_size))
        return tf.matmul(raw_output, self._pred_w)

def training_graph(seq_pred, input_batches, batch_size, unroll_depth):
    """Create unrolled graph with batches. Return (inputs, total_loss)."""
    print("Unrolling graph")
    # Switch time to be axis 0.
    input_characters = tf.transpose(input_batches, perm=[1, 0])
    input_characters = tf.reshape(
        input_characters, (unroll_depth, batch_size, 1))

    input_char_list = [
        input_characters[i] for i in xrange(unroll_depth)]

    out_logits, state = tf.nn.static_rnn(
        seq_pred,
        input_char_list,
        dtype=tf.float32)

    target_characters = input_characters
    target_onehot = tf.reshape(
        tf.one_hot(target_characters, 256, dtype=tf.float32),
        (unroll_depth, batch_size, 256))

    losses = tf.nn.softmax_cross_entropy_with_logits(
        labels=target_onehot[1:], logits=out_logits[:-1])

    total_loss = tf.reduce_mean(losses)
    return input_characters, total_loss

def gen_sequence(seq_pred):
    """Returns operations for sequence generation:
        (reset_state, predict_next, probabilities)"""
    pred_in = tf.get_variable("pred_input", (1, 1), dtype=tf.uint8)

    l_state = get_state_var(seq_pred, "pred_rnn_state")

    reset_state = tf.assign(
        l_state, 
        tf.convert_to_tensor(
            seq_pred.zero_state(1, dtype=tf.float32)))

    reset_pred = tf.assign(
            pred_in, tf.reshape(tf.cast(ord("\n"), tf.uint8), (1, 1)))
    reset = tf.group(
        reset_state,
        reset_pred)

    l_state_tuple = tensor_to_state_tuple(l_state)
    print(l_state_tuple)
    print(seq_pred.zero_state(1, dtype=tf.float32))
    out_logits, state = seq_pred(
        tf.reshape(pred_in, (1, 1, 1)), l_state_tuple)
        

    predictions = tf.nn.softmax(out_logits)[0]

    random_noise = tf.random_uniform((256,))
    rand_pred = tf.multiply(predictions, random_noise)
    selection = tf.argmax(rand_pred)

    next_prediction = tf.reshape(
        tf.cast(selection, dtype=tf.uint8), (1, 1))

    update_state = tf.assign(l_state, tf.reshape(state, l_state.shape))

    pick_next = tf.tuple(
        [tf.assign(pred_in, next_prediction)],
        control_inputs=[update_state])[0]
    
    return reset, pick_next

def train(data, batch_size, unroll_depth):
    lstm = create_lstm()
    seq_pred = SequencePredictor(lstm, 256, "SeqPred", "rnn")

    padded_length = int(math.ceil(len(data) / unroll_depth) * unroll_depth)
    data = np.concatenate(
        [data,
         [ord(" ")] * (padded_length - len(data))])

    input_text = tf.constant(data, dtype=tf.uint8)
    sequences = tf.train.batch([input_text], batch_size=unroll_depth,
                               capacity=1000,
                               enqueue_many=True)

    next_batch = tf.train.shuffle_batch(
        [sequences], batch_size=batch_size,
        capacity=1000, min_after_dequeue=600)
    
    # set up graph for training
    inputs, loss = training_graph(
        seq_pred, next_batch, batch_size, unroll_depth)

    # set up sequence prediction
    with tf.variable_scope("rnn"):
        print("Creating predictor")
        reset_predictor, predict_next = gen_sequence(seq_pred)

    global_step = tf.train.create_global_step()

    learning_rate = tf.train.inverse_time_decay(
        0.01, global_step, 2000, decay_rate=1)

    opt = tf.train.RMSPropOptimizer(learning_rate)
            
    opt_op = opt.minimize(loss / unroll_depth)

    print_loss = tf.Print(
        loss, [loss], "Loss: ")
    print_learning_rate = tf.Print(
        learning_rate, [learning_rate], "alpha: ")

    inc_step = tf.assign_add(global_step, 1)

    set_training = set_training_op()
    unset_training = unset_training_op()

    tf.get_variable_scope().reuse_variables()

    sv = tf.train.Supervisor(logdir=_LOG_DIR)
    with sv.managed_session() as sess:
        while not sv.should_stop():
            print('global_step: %s' % tf.train.global_step(sess, global_step))
            # Do some prediction:
            if sess.run(global_step) % 100 == 0:
                generated = []
                sess.run(reset_predictor)
                for i in xrange(5000):
                    next_char = sess.run(predict_next)
                    generated.append(chr(next_char[0, 0]))
                print("========")
                print(">>" + "".join(generated) + "<<")
                print("========")

            sess.run(set_training)
            sess.run(opt_op)

            sess.run(print_loss)
            sess.run(print_learning_rate)
            sess.run(inc_step)
            sess.run(unset_training)

def main():
    data = get_training_data()
    batch_size = 20
    unroll_depth = 200
    train(data, batch_size, unroll_depth)

if __name__ == "__main__":
    main()
