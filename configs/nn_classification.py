"""Generic classification config file."""
import tensorflow as tf
from tensorflow.estimator import ModeKeys

def model(mode, X_con, X_con_mask, X_cat, X_cat_mask, Y,
          image_indices, coordinates, metadata, utils):
    """
    Describe the specification of a Tensorflow custom estimator model.

    This function must be implemented in all configurations. It is almost
    exactly the model function passed to a custom Tensorflow estimator,
    apart from having more convenient input arguments.
    See https://www.tensorflow.org/guide/custom_estimators

    Parameters
    ----------
    features : dict
        Features is a recursive dictionary of tensors, proving the X inputs
        for the model (from the images). The dictonary has the following
        entries:
            indices -- (?, 2) the image coordinates of features
            coords -- (?, 2) the world coordinates (x, y) of features
            con -- dict of continuous feature columns
            cat -- dict of categorical feature columns
        Each con and cat feature is itself a dict, with two items:
            data -- the column data tensor
            mask -- the mask tensor
        The data and mask tensors are always of shape (?, p, p, 1)
        where p is the patch side length.
    labels : tf.Tensor
        A (?, k) tensor giving the k targets for the prediction.
    mode : tf.estimator.ModeSpec
        One of TRAIN, TEST or EVAL, describing in which context this code
        is being run by the estimator.
    params : dict
        Extra params given by the estimator. The critical one for configs
        is "metadata" that has comprehensive information about the features
        and targets useful for model building (for example, the number of
        possible values for each categorical column). For more details
        check the Landshark documentation.

    Returns
    -------
    tf.EstimatorSpec
        An EstimatorSpec object describing the model. For details check
        the Tensorflow custom estimator howto.

    """

    # Single-task classification
    nvalues_target = metadata.targets.nvalues[0]

    inputs_list = []
    if X_con:
        # let's 0-impute continuous columns
        X_con = {k: utils.value_impute(X_con[k], X_con_mask[k],
                                       tf.constant(0.0)) for k in X_con}

        # just concatenate the patch pixels as more features
        X_con = {k: utils.flatten_patch(v) for k, v in X_con.items()}

        # convenience function for catting all columns into tensor
        inputs_con = utils.continuous_input(X_con)
        inputs_list.append(inputs_con)

    if X_cat:
        X_cat = {k: utils.value_impute(X_cat[k], X_cat_mask[k],
                                       tf.constant(0)) for k in X_cat}
        X_cat = {k: utils.flatten_patch(v) for k, v in X_cat.items()}

        # zero is the missing categorical value so we can use it as extra category
        # some convenience functions for embedding / catting cols together
        # TODO explain the double zero
        nvalues = {k : v.nvalues[0][0] + 1
                   for k, v in metadata.features.categorical.columns.items()}
        embedding_dims = {k: 3 for k in X_cat.keys()}
        inputs_cat = utils.categorical_embedded_input(X_cat, nvalues,
                                                    embedding_dims)
        inputs_list.append(inputs_cat)


    # Build a simple 2-layer network
    inputs = tf.concat(inputs_list, axis=1)
    l1 = tf.layers.dense(inputs, units=64, activation=tf.nn.relu)
    l2 = tf.layers.dense(l1, units=32, activation=tf.nn.relu)

    # Get some predictions for the labels
    phi = tf.layers.dense(l2, units=nvalues_target,
                          activation=None)

    # geottiff doesn't support 64bit output from argmax
    predicted_classes = tf.cast(tf.argmax(phi, 1), tf.uint8)

    # Compute predictions.
    if mode == ModeKeys.PREDICT:
        predictions = {'predictions_{}'.format(
            metadata.targets.labels[0]): predicted_classes}
        return tf.estimator.EstimatorSpec(mode, predictions=predictions)

    # Use a loss for training
    Y = Y[:, 0]
    ll_f = tf.distributions.Categorical(logits=phi)
    loss = -1 * tf.reduce_mean(ll_f.log_prob(Y))
    tf.summary.scalar('loss', loss)

    # Compute evaluation metrics.
    acc = tf.metrics.accuracy(labels=Y, predictions=predicted_classes)
    metrics = {'accuracy': acc}

    if mode == ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(
            mode, loss=loss, eval_metric_ops=metrics)

    # For training, use Adam to learn
    assert mode == ModeKeys.TRAIN
    optimizer = tf.train.AdamOptimizer()
    train_op = optimizer.minimize(loss, global_step=tf.train.get_global_step())
    return tf.estimator.EstimatorSpec(mode, loss=loss, train_op=train_op)