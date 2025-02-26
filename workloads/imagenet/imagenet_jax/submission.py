"""Training algorithm track submission functions for ImageNet."""

import functools
from typing import Iterator, List, Tuple

import jax
import jax.numpy as jnp
import optax
from flax import jax_utils
from jax import lax

import spec



def get_batch_size(workload_name):
  del workload_name
  return 128


def cosine_decay(lr, step, total_steps):
  ratio = jnp.maximum(0., step / total_steps)
  mult = 0.5 * (1. + jnp.cos(jnp.pi * ratio))
  return mult * lr


def create_learning_rate_fn(
    hparams: spec.Hyperparamters,
    steps_per_epoch: int):
  """Create learning rate schedule."""
  base_learning_rate = hparams.learning_rate * get_batch_size('imagenet') / 256.
  warmup_fn = optax.linear_schedule(
      init_value=0., end_value=base_learning_rate,
      transition_steps=hparams.warmup_epochs * steps_per_epoch)
  cosine_epochs = max(hparams.num_epochs - hparams.warmup_epochs, 1)
  cosine_fn = optax.cosine_decay_schedule(
      init_value=base_learning_rate,
      decay_steps=cosine_epochs * steps_per_epoch)
  schedule_fn = optax.join_schedules(
      schedules=[warmup_fn, cosine_fn],
      boundaries=[hparams.warmup_epochs * steps_per_epoch])
  return schedule_fn


def optimizer(hyperparameters: spec.Hyperparamters, num_train_examples: int):
  steps_per_epoch = num_train_examples // get_batch_size('imagenet')
  learning_rate_fn = create_learning_rate_fn(hyperparameters, steps_per_epoch)
  opt_init_fn, opt_update_fn = optax.sgd(
      nesterov=True,
      momentum=hyperparameters.momentum,
      learning_rate=learning_rate_fn
    )
  return opt_init_fn, opt_update_fn


def init_optimizer_state(
    workload: spec.Workload,
    model_params: spec.ParameterContainer,
    model_state: spec.ModelAuxiliaryState,
    hyperparameters: spec.Hyperparamters,
    rng: spec.RandomState) -> spec.OptimizerState:
  params_zeros_like = jax.tree_map(
      lambda s: jnp.zeros(s.shape_tuple), workload.param_shapes)
  opt_init_fn, opt_update_fn = optimizer(
      hyperparameters, workload.num_train_examples)
  optimizer_state = opt_init_fn(params_zeros_like)
  return jax_utils.replicate(optimizer_state), opt_update_fn


# We need to jax.pmap here instead of inside update_params because the latter
# would recompile the function every step.
@functools.partial(
  jax.pmap,
  axis_name='batch',
  in_axes=(None, None, 0, 0, 0, None, 0, None),
  static_broadcasted_argnums=(0, 1))
def pmapped_train_step(workload, opt_update_fn, model_state, optimizer_state,
                       current_param_container, hyperparameters, batch, rng):
  def _loss_fn(params):
    """loss function used for training."""
    variables = {'params': params, **model_state}
    logits, new_model_state = workload.model_fn(
        params,
        batch,
        model_state,
        spec.ForwardPassMode.TRAIN,
        rng,
        update_batch_norm=True)
    loss = workload.loss_fn(batch['label'], logits)
    weight_penalty_params = jax.tree_leaves(variables['params'])
    weight_l2 = sum([jnp.sum(x ** 2)
                    for x in weight_penalty_params
                    if x.ndim > 1])
    weight_penalty = hyperparameters.l2 * 0.5 * weight_l2
    loss = loss + weight_penalty
    return loss, (new_model_state, logits)

  grad_fn = jax.value_and_grad(_loss_fn, has_aux=True)
  aux, grad = grad_fn(current_param_container)
  grad = lax.pmean(grad, axis_name='batch')
  new_model_state, logits = aux[1]
  updates, new_optimizer_state = opt_update_fn(
      grad, optimizer_state, current_param_container)
  updated_params = optax.apply_updates(current_param_container, updates)

  return new_model_state, new_optimizer_state, updated_params


def update_params(
    workload: spec.Workload,
    current_param_container: spec.ParameterContainer,
    current_params_types: spec.ParameterTypeTree,
    model_state: spec.ModelAuxiliaryState,
    hyperparameters: spec.Hyperparamters,
    input_batch: spec.Tensor,
    label_batch: spec.Tensor,
    loss_type: spec.LossType,
    optimizer_state: spec.OptimizerState,
    eval_results: List[Tuple[int, float]],
    global_step: int,
    rng: spec.RandomState) -> spec.UpdateReturn:
  """Return (updated_optimizer_state, updated_params, updated_model_state)."""
  batch = {
    'image': input_batch,
    'label': label_batch
  }
  optimizer_state, opt_update_fn = optimizer_state
  new_model_state, new_optimizer_state, new_params = pmapped_train_step(
    workload, opt_update_fn, model_state, optimizer_state,
    current_param_container, hyperparameters, batch, rng)

  steps_per_epoch = workload.num_train_examples // get_batch_size('imagenet')
  if (global_step + 1) % steps_per_epoch == 0:
    # sync batch statistics across replicas once per epoch
    new_model_state = workload.sync_batch_stats(new_model_state)

  return (new_optimizer_state, opt_update_fn), new_params, new_model_state


def data_selection(
    workload: spec.Workload,
    input_queue: Iterator[Tuple[spec.Tensor, spec.Tensor]],
    optimizer_state: spec.OptimizerState,
    current_param_container: spec.ParameterContainer,
    hyperparameters: spec.Hyperparamters,
    global_step: int,
    rng: spec.RandomState) -> Tuple[spec.Tensor, spec.Tensor]:
  """Select data from the infinitely repeating, pre-shuffled input queue.

  Each element of the queue is a single training example and label.

  Return a tuple of input label batches.
  """
  x = next(input_queue)
  return x['image'], x['label']
