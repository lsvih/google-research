# coding=utf-8
# Copyright 2021 The Google Research Authors.
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

"""Pipeline utils."""

import copy
import functools
import os

from absl import logging
import flax
from flax import jax_utils
from flax import optim
from flax.deprecated import nn
from flax.training import checkpoints
import jax
import jax.numpy as jnp
import ml_collections
import numpy as onp
import tensorflow as tf

from gift.models import all_models
from gift.models import model_utils
from gift.tasks import domain_mapping_utils
from gift.train_lib import optimizers
from gift.utils import tensor_util


@flax.struct.dataclass
class TrainState:
  """Dataclass to keep track of state of training.

  The state of training is structured as a flax.struct.dataclass, which enables
  instances of this class to be passed into jax transformations like tree_map
  and pmap.
  """
  global_step: int
  optimizer: optim.Optimizer
  model_state: nn.Collection
  rng: jnp.ndarray

  def clone(self):
    """Deep copy a TrainState object.

    Returns:
      Cloned TrainState
    """
    new_train_state = jax.tree_map(copy.deepcopy, self)
    return new_train_state


def get_num_training_steps(hparams, dataset_metadata):
  """Calculates the total number of training steps and possibly steps_per_epoch.

  The main training loop is based on the number of training steps. Thus, for
  datasets that we want to train based on the number of epochs, we need to
  calculate
  the total number of training steps. This function looks for
  `num_training_steps` in hparams, if it exists it returns that as the total
  step and `None` as `steps_per_epoch`. If num_training_steps doesn't exist,
  then it looks for `num_training_epochs` and given the size of training data
  calculates the total steps and steps_per_epoch. In this computation, we assume
  that drop_remainder=True.

  Args:
    hparams: Hyperparameters.
    dataset_metadata: Meta-data that is generated by the dataset_builder.

  Returns:
    total_steps: int; Total number of training steps.
    steps_per_epoch: int or None; number of steps in every epoch.
  """
  # we either use num_training_epochs or num_training_steps
  if hparams.get('num_training_steps'):
    assert not hparams.get('num_training_epochs')
    return hparams.num_training_steps, None
  else:
    assert hparams.num_training_epochs and not hparams.get('num_training_steps')
    steps_per_epoch = dataset_metadata[
        'num_train_examples'] // hparams.batch_size
    return (steps_per_epoch * hparams.num_training_epochs), steps_per_epoch


@functools.partial(jax.pmap, axis_name='x')
def pmap_mean(x):
  return jax.lax.pmean(x, 'x')


def sync_model_state_across_replicas(train_state):
  """Sync the model_state (like batch statistics) across replicas.

  Args:
    train_state: TrainState; Current state of training.

  Returns:
    Updated state of training in which model_state is synced across replicas.
  """
  # TODO(samiraabnar): Fix sync for different kinds of statistics.
  logging.warning('All model state statistics are averaged during syncing'
                  'across replicas.')

  if jax.tree_leaves(train_state.model_state):
    # if the model_state is not empty
    return train_state.replace(model_state=pmap_mean(train_state.model_state))
  else:
    return train_state


def restore_checkpoint(experiment_dir, train_state, step=None):
  """Restores the last checkpoint.

  First restores the checkpoint, which is an instance of TrainState that holds
  the state of training, and then replicates it.

  Args:
    experiment_dir: str; Experiment directory for saving the checkpoint.
    train_state: Dataclass; An instance of TrainState that holds the state of
      training.
    step: int; Checkpoint step to load.

  Returns:
    training state and an int which is the current step.
  """
  train_state = checkpoints.restore_checkpoint(experiment_dir, train_state,
                                               step)
  current_step = int(train_state.global_step)
  return train_state, current_step


def checkpoint_path(ckpt_dir, step, prefix='checkpoint_'):
  return os.path.join(ckpt_dir, f'{prefix}{step}')


def save_checkpoint(experiment_dir, train_state, keep=3):
  """Saves a checkpoint.

  First syncs the model state across replicas, then it unreplicates it by taking
  the train state of the first replica and saves it as a checkpoint.

  Args:
    experiment_dir: str; Experiment directory for saving the checkpoint.
    train_state: Dataclass; An instance of TrainState that holds the state of
      training.
    keep: int; Number of checkpoints to keep.
  """
  if jax.host_id() == 0:
    # get train state from the first replica
    checkpoint_state = jax.device_get(jax_utils.unreplicate(train_state))
    ckpt_path = checkpoint_path(experiment_dir,
                                int(checkpoint_state.global_step))
    if not tf.io.gfile.exists(ckpt_path):
      checkpoints.save_checkpoint(
          experiment_dir,
          checkpoint_state,
          int(checkpoint_state.global_step),
          keep=keep)


def bind_rng_to_host_device(rng, axis_name, bind_to=None):
  """Binds a rng to the host/device we are on.

  Must be called from within a pmapped function.

  Args:
    rng: A jax.random.PRNGKey.
    axis_name: str; The axis of the devices we are binding rng across.
    bind_to: list; Must be a list that may contain 'host', 'device'.

  Returns:
    jax.random.PRNGKey specialized to host/device.
  """
  if bind_to is None:
    bind_to = ['host', 'device']
  for entry in bind_to:
    assert entry in ['host', 'device']
  if 'host' in bind_to:
    rng = jax.random.fold_in(rng, jax.host_id())
  if 'device' in bind_to:
    rng = jax.random.fold_in(rng, jax.lax.axis_index(axis_name))
  return rng


class TrainingDivergedError(Exception):
  pass


# We want all parameters to be created in host RAM, not on any device, they'll
# be sent there later as needed, otherwise we already encountered two
# situations where we allocate them twice.
def create_flax_module(flax_module_def, input_shape, hparams, rng,
                       model_input_dtype):
  """Creates Flax module by initializing its parameters.

  Args:
    flax_module_def: definition of a Flax module.
    input_shape: tuple; Shape of input.
    hparams: ConfigDitct; Hyper parameters.
    rng: Jax rng key.
    model_input_dtype: Model input data type.

  Returns:
    A created flax_model and the initial model_state.
  """

  @functools.partial(jax.jit, backend='cpu')
  def _create_flax_module():
    device_batch_size = hparams.batch_size // jax.device_count()
    shape = (device_batch_size,) + tuple(input_shape[1:])
    model_rng, init_rng = jax.random.split(rng)
    with nn.stateful() as init_model_state:
      with nn.stochastic(model_rng):
        _, initial_params = flax_module_def.init_by_shape(
            init_rng, [(shape, model_input_dtype)])
    flax_module = nn.Model(flax_module_def, initial_params)
    num_trainable_params = model_utils.log_param_shapes(flax_module)
    return flax_module, init_model_state, num_trainable_params

  return _create_flax_module()


def eval_step(train_state, batch, metrics_fn):
  """Runs a single step of training.

  Args:
    train_state: TrainState, the state of training including the current
      global_step, model_state, rng, and optimizer.
    batch: A single batch of data. a metrics function, that given logits and
      batch of data, calculates the metrics as well as the loss.
    metrics_fn: A metrics function, that given logits and batch of data,
      calculates the metrics as well as the loss.

  Returns:
    Calculated metrics.
  """
  flax_module = train_state.optimizer.target
  with nn.stateful(train_state.model_state, mutable=False):
    logits = flax_module(batch['inputs'], train=False)
  metrics = metrics_fn(logits, batch)
  return metrics


def pseudo_label_generator(batch,
                           train_state,
                           pseudo_labels_transformer_fn=lambda x: (x, None),
                           input_key='inputs',
                           train=True):
  """Pseudo label generator passed to the dataset class.

  This function can be passed to datasets initializer for self-supervised
   training or distillation.

  Args:
    batch: dict; Batch of examples, witch an 'inputs' key.
    train_state: TrainState; Train state of the model which we want to use to
      generate pseudo labels.
    pseudo_labels_transformer_fn: function; A function that applies a specific
      transformation on the logits from the model to generate the labels. The
      most basic function to be used here is a simple softmax or argmax to get
      one-hot labels. This function should return the labels and the weights for
      each example in the batch (for each label) and has the following API: ```
        new_labels, weights = pseudo_labels_transformer(logits) ```
    input_key: str; What key to use to retrieve the input field of the batch.
    train: bool; Train flag passed to the model forward pass.

  Returns:
    Return the batch with ground truth labels and weights replaced with
    pseudo labels and new weights.
  """
  inputs = batch[input_key]
  _, dropout_rng = jax.random.split(train_state.rng)

  with nn.stochastic(dropout_rng):
    with nn.stateful(train_state.model_state):
      logits = train_state.optimizer.target(inputs, train=train)
      # Make sure the parameter of the teacher are not updated.

      logits = jax.lax.stop_gradient(logits)

      batch['label'], weights = pseudo_labels_transformer_fn(logits)

      if weights is not None:
        batch['weights'] = weights

  return batch


def load_model(rng,
               model_config,
               model_ckpt,
               task,
               load_full_train_state=True,
               checkpoint_step=None):
  """Set up a train state model and loads it from the given checkpoint path.

  Args:
    rng: float; JAX PRNG key.
    model_config: configdict; Hparams of the model.
    model_ckpt: str; Path to model checkpoint.
    task: Task; Task on the which the model will be applied.
    load_full_train_state: bool; Whether to load the full TrainState or just the
      model and model_state.
    checkpoint_step: int; Checkpoint step to load (if None loads the most recent
      checkpoint).

  Returns:
    TrainState if load_full_train_state else (model, model_state).
  """
  teacher_cls = all_models.get_model_class(model_config.get('model_name'))

  model_config.output_dim = task.task_params.output_dim
  flax_module, _ = teacher_cls.build_flax_module(model_config)

  # Initialize flax model.
  rng, dropout_rng = jax.random.split(rng)
  (flax_model, init_model_state, _) = create_flax_module(
      flax_module, task.dataset.meta_data['input_shape'], model_config,
      dropout_rng, task.dataset.meta_data.get('input_dtype', jnp.float32))

  if load_full_train_state:
    # Create train state.
    rng, teacher_rng = jax.random.split(rng)
    train_state = TrainState(
        global_step=0,
        optimizer=optimizers.get_optimizer(model_config).create(flax_model),
        model_state=init_model_state,
        rng=teacher_rng)

    # Load from checkpoint if checkpoint is specified.
    if model_ckpt:
      train_state, start_step = restore_checkpoint(model_ckpt, train_state,
                                                   checkpoint_step)
      logging.info('Loading model checkpoint at step %d', start_step)

    return train_state
  elif model_ckpt:
    model, model_state = checkpoints.restore_checkpoint(
        model_ckpt, (flax_model, init_model_state), checkpoint_step)

    return model, model_state


def logit_transformer(logits,
                      temp=1.0,
                      confidence_quantile_threshold=1.0,
                      self_supervised_label_transformation='soft',
                      logit_indices=None):
  """Transforms logits into labels used as targets in a loss functions.

  Args:
    logits: jnp float array; Prediction of a model.
    temp: float; Softmax temp.
    confidence_quantile_threshold: float; Training examples are weighted based
      on this.
    self_supervised_label_transformation: str; Type of labels to produce (soft
      or sharp).
    logit_indices: list(int); Usable Indices for logits (list of indices to
      use).

  Returns:

  """
  # Compute confidence for each prediction:
  confidence = jnp.amax(logits, axis=-1) - jnp.amin(logits, axis=-1)

  # Compute confidence threshold:
  alpha = jnp.quantile(confidence, confidence_quantile_threshold)
  # Only train on confident outputs:
  weights = jnp.float32(confidence >= alpha)

  if self_supervised_label_transformation == 'sharp':
    if logit_indices:
      logits = logits[Ellipsis, logit_indices]
    new_labels = jnp.argmax(logits, axis=-1)
  elif self_supervised_label_transformation == 'soft':
    new_labels = nn.softmax(logits / (temp or 1.0), axis=-1)
  else:
    new_labels = logits

  return new_labels, weights


def get_pseudo_label_generator(train_state, input_key, train,
                               **label_transformer_params):
  """Get the pseudo label generator function."""
  pseudo_label_generator_fn = functools.partial(
      pseudo_label_generator,
      input_key=input_key,
      pseudo_labels_transformer_fn=functools.partial(
          logit_transformer, **label_transformer_params),
      train=train)

  return functools.partial(
      jax.pmap(pseudo_label_generator_fn), train_state=train_state)


@functools.partial(jax.vmap, in_axes=(None, 0))
def vmapped_flax_module_eval(flax_module, env_batch):
  """Vmapped forward pass of flax_module (with train flag == False).

  Args:
    flax_module: flax.nn.Model; Flax model.
    env_batch: dict; A batch of examples.

  Returns:
    logits.
  """

  return flax_module(env_batch, train=False)


@functools.partial(jax.vmap, in_axes=(None, None, 0))
def vmapped_flax_module_train(flax_module, model_state, env_batch):
  """Vmapped forward pass of flax_module (with train flag == True).

  Args:
    flax_module: flax.nn.Model; Flax model.
    model_state: flax.nn.Collection; Model state.
    env_batch: dict; A batch of examples.

  Returns:
    logits.
  """
  with nn.stateful(model_state) as new_model_state:
    return flax_module(
        env_batch, train=True, return_activations=False), new_model_state


@functools.partial(jax.vmap, in_axes=(0, None, None, None, None))
def vmapped_flax_module_with_reps(env_batch, flax_module, model_state,
                                  input_layer_key, train):
  """Vmapped forward pass of flax_module.

  Args:
    env_batch: dict; A batch of examples.
    flax_module: flax.nn.Model; Flax model.
    model_state: flax.nn.Collection; Model state.
    input_layer_key: str; Which layer the input should be plugged in.
    train: bool; Train flag.

  Returns:
    logits, hidden activations, activations of key layer, and new model state.
  """

  return forward_pass_with_reps(env_batch, flax_module, model_state,
                                input_layer_key, train)


@functools.partial(jax.vmap, in_axes=(0, None, None, None, None))
def vmapped_dann_flax_module(env_batch, flax_module, model_state,
                             input_layer_key, train):
  """Vmapped forward pass of flax_module.

  Args:
    env_batch: dict; A batch of examples.
    flax_module: flax.nn.Model; Flax model.
    model_state: flax.nn.Collection; Model state.
    input_layer_key: str; Which layer the input should be plugged in.
    train: bool; Train flag.

  Returns:
    logits, hidden activations, activations of key layer, and new model state.
  """

  return dann_forward_pass(env_batch, flax_module, model_state, input_layer_key,
                           train)


def forward_pass_with_reps(
    batch,
    flax_module,
    model_state,
    input_layer_key,
    train,
):
  """Forward pass of flax_module.

  Args:
    batch: dict; A batch of examples.
    flax_module: flax.nn.Model; Flax model.
    model_state: flax.nn.Collection; Model state.
    input_layer_key: str; Which layer the input should be plugged in.
    train: bool; Train flag.

  Returns:
    logits, hidden activations, activations of key layer, and new model state.
  """
  with nn.stateful(model_state) as new_model_state:
    logits, reps, reps_key = flax_module(
        batch,
        train=train,
        return_activations=True,
        input_layer_key=input_layer_key)

    key_reps = reps[reps_key]

    return logits, reps, key_reps, new_model_state


def dann_forward_pass(
    batch,
    flax_module,
    model_state,
    input_layer_key,
    train,
):
  """Forward pass of flax_module for DANN.

  Args:
    batch: dict; A batch of examples.
    flax_module: flax.nn.Model; Flax model.
    model_state: flax.nn.Collection; Model state.
    input_layer_key: str; Which layer the input should be plugged in.
    train: bool; Train flag.

  Returns:
    logits, hidden activations, activations of key layer, and new model state.
  """
  with nn.stateful(model_state) as new_model_state:
    logits, reps, reps_key, domain_logits = flax_module(
        batch,
        train=train,
        return_activations=True,
        input_layer_key=input_layer_key,
        discriminator=True)

    key_reps = reps[reps_key]

    return logits, domain_logits, reps, key_reps, new_model_state


def get_inputs(batch, input_key):
  """Returns input field of the given batch.

  This function is defined to avoid defining lambda functions when calling map
  function to get the inputs if a list/dict of batches.

  Args:
    batch: dict; Dictionary of examples (with inputs and labels key).
    input_key: str; Key for the input (inputs == batch[input_key]).
  """
  return batch[input_key]


@functools.partial(jax.jit, static_argnums=(1,))
def get_multi_env_inputs(env_batches, input_key='inputs'):
  """List(Batches) --> List(Batches[input_key]).

  Args:
    env_batches: list(dict); List of batches, where each batch is a dictionary.
    input_key: str; Key for the input (inputs == batch[input_key]).

  Returns:

  """
  return jnp.array(
      list(
          map(functools.partial(get_inputs, input_key=input_key), env_batches)))


def compute_global_mean_metrics(metrics_dict):
  """Computes average of the metrics across all devices.

  Args:
    metrics_dict: dict; metric_name --> metric value(s), (normalization factor).
      for some metrics, e.g. learning_rate it is just a scalar value.

  Returns:
    Averaged metrics.
  """
  metrics = {}
  for key in metrics_dict:
    if isinstance(metrics_dict[key], tuple):
      # If val is tuple of (value, normalizer), for most of the metrics
      # this is the case.
      if onp.sum(metrics_dict[key][1]) > 0:
        metrics[key] = onp.sum(metrics_dict[key][0]) / onp.sum(
            metrics_dict[key][1])
      else:
        metrics[key] = 0.
    else:
      # If it is not a tuple, for example learning rate does not have
      # a normalizer.
      metrics[key] = onp.mean(metrics_dict[key])
  return metrics


def get_self_matching_matrix(batch,
                             reps,
                             mode='random',
                             label_cost=1.0,
                             l2_cost=1.0):
  """Align examples in a batch.

  Args:
    batch: list(dict); Batch of examples (with inputs, and label keys).
    reps: list(jnp array); List of representations of a selected layer for each
      batch.
    mode: str; Determines alignment method.
    label_cost: float; Weight of label cost when Sinkhorn matching is used.
    l2_cost: float; Weight of l2 cost when Sinkhorn matching is used.

  Returns:
    Matching matrix with shape `[num_batches, batch_size, batch_size]`.
  """
  if mode == 'random':
    number_of_examples = batch['inputs'].shape[0]
    rng = nn.make_rng()
    matching_matrix = jnp.eye(number_of_examples)
    matching_matrix = jax.random.permutation(rng, matching_matrix)
  elif mode == 'sinkhorn':
    epsilon = 0.1
    num_iters = 100

    reps = reps.reshape((reps.shape[0], -1))
    x = y = reps
    x_labels = y_labels = batch['label']

    # Solve sinkhorn in log space.
    num_x = x.shape[0]
    num_y = y.shape[0]

    # Marginal of rows (a) and columns (b)
    a = jnp.ones(shape=(num_x,), dtype=x.dtype)
    b = jnp.ones(shape=(num_y,), dtype=y.dtype)
    cost = domain_mapping_utils.pairwise_l2(x, y)
    cost += jnp.eye(num_x) * jnp.max(cost) * 10

    # Adjust cost such that representations with different labels
    # get assigned a very high cost.
    same_labels = domain_mapping_utils.pairwise_equality_1d(x_labels, y_labels)

    adjusted_cost = (1 - same_labels) * label_cost + l2_cost * cost
    _, matching, _ = domain_mapping_utils.sinkhorn_dual_solver(
        a, b, adjusted_cost, epsilon, num_iters)

    matching_matrix = domain_mapping_utils.round_coupling(
        matching, jnp.ones((matching.shape[0],)), jnp.ones(
            (matching.shape[1],)))
  else:
    raise ValueError('%s mode for self matching alignment is not supported.' %
                     mode)
  return matching_matrix


def interpolate(rng,
                matching_matrix,
                reps1,
                reps2,
                num_lambdas,
                alpha=1.0,
                beta=1.0,
                lmbda=-1,
                interpolation_method=tensor_util.convex_interpolate):
  """Computes interpolation between reps1 and reps1.

  Args:
    rng: JAX PRNG key.
    matching_matrix: jnp array; Alignment matrix.
    reps1: jnp array; Tensor with shape [..., feature_size].
    reps2: jnp array; Tensor with shape [..., feature_size]
    num_lambdas: int; Number of interpolations per pair.
    alpha: float; Parameter of the beta distribution which lambdas are sampled
      from.
    beta: float; Parameter of the beta distribution which lambdas are sampled
      from.
    lmbda: float; If not -1, it will be used as the interpolation coefficient
      (there will be no sampling from the beta distribution). The reasonable
      range for lmbda is [0,1].
    interpolation_method: fn(x, y, lmbda); Function used for interpolating.

  Returns:
    Interpolated reps, and sampled lambdas (interpolation coefficients).
  """
  assert reps1.shape == reps2.shape, ('To be interpolated reps should have '
                                      'similar shape.')

  # Get interpolation target, if matching_matrix is one-hot, this simply returns
  # the selected rows of reps2.
  reps2 = jnp.einsum('ij,j...->i...', matching_matrix, reps2)

  # Sample lambda from [0,1] for interpolation:
  def sample_beta(_):
    return jax.random.beta(
        rng, a=alpha, b=beta, shape=(num_lambdas, len(reps1)))

  sample_lambdas = jax.lax.cond(
      lmbda != -1,
      lambda _: jnp.ones((num_lambdas, len(reps1))) * lmbda,
      sample_beta,
      operand=None)

  # Compute the interpolated states:
  new_reps = interpolation_method(reps1, reps2, sample_lambdas)

  return new_reps, sample_lambdas


def sample_layer(layer_keys, mixup_layer_range=None, mixup_layers=None):
  """Sample a layer and return the sampled layer name.

  Args:
    layer_keys: list(str); List of layer names.
    mixup_layer_range: tuple; Indicating range of mixup layers.
    mixup_layers: list; List of layer names, from which mixup layer should be
      sampled.

  Returns:

  """
  assert not (mixup_layer_range and mixup_layers), (
      'Only one of mixup_layer_range and mixup_layers should be set.')

  if mixup_layer_range is not None:
    max_mixup_layer = mixup_layer_range[1]
    min_mixup_layer = mixup_layer_range[0]
    min_layer = onp.maximum(max_mixup_layer, 0)
    max_layer = onp.minimum(min_mixup_layer, len(layer_keys))
    layers = onp.arange(min_layer, max_layer)
  elif mixup_layers is not None:
    layers = mixup_layers
  else:
    raise ValueError('One of mixup_layer_range and mixup_layers should be set.')

  sampled_layer = layer_keys[int(
      onp.random.choice(layers, replace=True, p=None))]
  return sampled_layer


def scheduler(step, params):
  """Scheduler for any arbitrary parameter.

  Args:
    step: int; Training step.
    params: dict; Scheduling parameters.

  Returns:
    value of the parameter in the given step.
  """
  if params['mode'] == 'constant':
    # ----------------
    return params['initial_value']

  elif params['mode'] == 'linear_decay':
    #  \
    #   \_____________
    num_steps = params.get('num_steps', 1)
    step_size = params['total_steps'] / (num_steps)

    if num_steps > 1:
      decay_size = (params['initial_value'] - params['min_value']) / (
          num_steps - 1)
    else:
      decay_size = 0

    value = params['initial_value'] - ((step // step_size)) * decay_size

    if params.get('min_value') is not None:
      value = jnp.maximum(params['min_value'], value)

    return value

  elif params['mode'] == 'linear_grow':
    #   _______________
    #  /
    # /
    num_steps = params.get('num_steps', 1)
    step_size = params['total_steps'] / (num_steps)

    if num_steps > 1:
      decay_size = float(params['max_value'] - params['initial_value']) / (
          num_steps - 1)
    else:
      decay_size = 0

    value = ((step // step_size)) * decay_size

    if params.get('max_value') is not None:
      value = jnp.minimum(params['max_value'], value)

    return value


def get_sample_layer_params(hparams, all_env_reps):
  """Returns inputs parameter for sample layer function."""
  layer_keys = list(all_env_reps.keys())
  mixup_layers = hparams.get('mixup_layer_set', None)
  if mixup_layers is None:
    min_layer = jnp.maximum(hparams.get('min_mixup_layer', 0), 0)
    max_layer = jnp.minimum(
        hparams.get('max_mixup_layer', len(layer_keys)), len(layer_keys))
    mixup_layers = jnp.arange(min_layer, max_layer)

  return layer_keys, mixup_layers


def get_weight_param(hparams, param_name, default_value=1.0):
  """Returns params dict for a parameter value based on what is set in hparams.

  Args:
    hparams: configDict.
    param_name: str; Name of the weight parameter.
    default_value: float; Value used if not set.
  """
  default_params = {
      'initial_value': hparams.get(param_name, default_value),
      'mode': 'constant'
  }
  params = hparams.get(param_name + '_params', default_params)

  return params


def load_teacher_info(hparams):
  """Load teacher config and checkpoint path."""
  teacher_config = ml_collections.ConfigDict(
      hparams.teacher.get('teacher_config', {}))
  teacher_ckpnt = hparams.teacher.get('teacher_ckpt', None)
  teacher_ckpnt_step = hparams.teacher.get('checkpoint_step', None)

  return teacher_config, teacher_ckpnt, teacher_ckpnt_step
