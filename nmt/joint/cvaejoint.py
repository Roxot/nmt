"""
:Authors: - Bryan Eikema
"""

import tensorflow as tf

import nmt.utils.misc_utils as utils

from . import CSimpleJointModel, DVAEJointModel
from nmt import model_helper

class CVAEJointModel(CSimpleJointModel, DVAEJointModel):

  def __init__(self, hparams, mode, iterator, source_vocab_table,
               target_vocab_table, reverse_target_vocab_table=None,
               scope=None, extra_args=None):

    super(CVAEJointModel, self).__init__(hparams=hparams, mode=mode,
        iterator=iterator, source_vocab_table=source_vocab_table,
        target_vocab_table=target_vocab_table,
        reverse_target_vocab_table=reverse_target_vocab_table,
        scope=scope, extra_args=extra_args)

  # Overrides CSimpleJointModel.build_graph
  def build_graph(self, hparams, scope=None):
    utils.print_out("# creating %s graph ..." % self.mode)
    dtype = tf.float32

    with tf.variable_scope(scope or "dynamic_seq2seq", dtype=dtype):

      # Infer z from the embeddings
      Z = self._infer_z_from_embeddings(hparams)
      z_sample = Z.sample()

      with tf.variable_scope("generative_model", dtype=dtype):

        # P(x_1^m) language model
        gauss_observations = self._build_language_model(hparams,
            z_sample=z_sample)

        # P(y_1^n|x_1^m) encoder
        encoder_outputs, encoder_state = self._build_encoder(hparams,
            z_sample=z_sample)

        # P(y_1^n|x_1^m) decoder
        tm_logits, sample_id, final_context_state = self._build_decoder(
            encoder_outputs, encoder_state, hparams, z_sample=z_sample)

        # Loss
        if self.mode != tf.contrib.learn.ModeKeys.INFER:
          with tf.device(model_helper.get_device_str(self.num_encoder_layers - 1,
                                                     self.num_gpus)):
            loss = self._compute_loss(tm_logits, gauss_observations, Z)
        else:
          loss = None

    return tm_logits, loss, final_context_state, sample_id

  # Overrides CSimpleJointModel._compute_loss
  def _compute_loss(self, tm_logits, gauss_observations, Z):

    # - E_Qx[ E_qz[ log P(y_1^n | x_1^m, z) ] ]
    tm_loss = self._compute_categorical_loss(tm_logits,
        self.target_output, self.target_sequence_length)

    # - E_Qx[ E_qx [ log p(x_1^m | z) ] ]
    lm_loss = self._gaussian_nll(gauss_observations, self.source_output,
        self.source_sequence_length)

    # We compute an analytical KL between the Gaussian variational approximation
    # and its standard Gaussian prior.
    standard_normal = tf.contrib.distributions.MultivariateNormalDiag(
        tf.zeros_like(Z.mean()), tf.ones_like(Z.stddev()))
    KL_Z = Z.kl_divergence(standard_normal)
    KL_Z = tf.reduce_mean(KL_Z)

    # H(X|y_1^n) -- keep in mind self.Qx is defined in batch major, as are all
    # data streams.
    entropy = tf.cond(self.mono_batch,
        true_fn=lambda: tf.reduce_mean(tf.reduce_sum(self.Qx.entropy(), axis=1)),
        false_fn=lambda: tf.constant(0.))

    return tm_loss + lm_loss + KL_Z - entropy
