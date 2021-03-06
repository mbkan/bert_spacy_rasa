#!/usr/bin/env python
from collections import Counter

import plac
import re
import random
import json
import spacy
import torch
import tqdm
import unicodedata
import logging.config
import numpy as np
import pandas as pd
from pathlib import Path

import wasabi
from sklearn.model_selection import train_test_split
from spacy.util import minibatch
from spacy_transformers.util import cyclic_triangular_rate

logging.config.fileConfig('logging.conf')
logger = logging.getLogger(__name__)


@plac.annotations(
    model=("Model name", "positional", None, str),
    output_dir=("Optional output directory", "option", "o", Path),
    batch_size=("Number of docs per batch", "option", "bs", int),
    learn_rate=("Learning rate", "option", "lr", float),
    n_iter=("Number of training epochs", "option", "n", int),
    sentence_split=("If the script should split docs into their sentences", "option", "s", bool),
)
def main(
    model,
    output_dir=None,
    n_iter=4,
    batch_size=24,
    learn_rate=2e-5,
    sentence_split=False
):
    """

    :param model:
    :param output_dir:
    :param n_iter:
    :param batch_size:
    :param learn_rate:
    :return:
    """

    max_wpb = 1000

    spacy.util.fix_random_seed(0)
    is_using_gpu = spacy.prefer_gpu()
    if is_using_gpu:
        torch.set_default_tensor_type("torch.cuda.FloatTensor")
    if output_dir is not None:
        output_dir = Path(output_dir)
        if not output_dir.exists():
            output_dir.mkdir()

    # Creating the output directory if it's not already present
    if output_dir is not None:
        output_dir = Path(output_dir)
        if not output_dir.exists():
            output_dir.mkdir()

    # Load the pretrained BERT with spacy and check the pipeline names
    nlp = spacy.load(model)
    logger.info('Loaded model: {}'.format(model))
    logger.info('Loaded models pipeline names: {}'.format(nlp.pipe_names))

    # Using a softmax pooler output as first approach.
    textcat = nlp.create_pipe(
        "trf_textcat", config={"architecture": "softmax_pooler_output", "words_per_batch": max_wpb}
    )

    # Loading the 10kGNAD dataset with pandas, representing its labels as a list
    logger.info("Loading domain specific data...")
    df_train_set = pd.read_csv('data/articles.csv', delimiter=';', error_bad_lines=False, names=['label', 'article'])
    train_label_list = df_train_set['label'].unique().tolist()

    logger.info(
        f"Using {len(df_train_set)} training docs overall.")

    # Do a stratified train test split and persist the result for later usage
    train_dataframe, eval_dataframe_first = train_test_split(df_train_set, test_size=0.4, stratify=df_train_set['label'])
    eval_dataframe, dev_dataframe = train_test_split(eval_dataframe_first, test_size=0.5, stratify=eval_dataframe_first['label'])

    train_data = list(train_dataframe.itertuples(index=False, name=None))
    test_data = list(eval_dataframe.itertuples(index=False, name=None))
    dev_data = list(dev_dataframe.itertuples(index=False, name=None))

    logger.info(f"Using {len(train_data)} training docs, {len(test_data)} evaluation) and {len(dev_data)} for development.")

    # Some of the evaluation scripts are loading JSON so we are going to provide the split as JSON aswell
    with open('data/train.json', 'w') as handle:
        json.dump(train_data, handle)

    with open('data/test.json', 'w') as handle:
        json.dump(test_data, handle)

    with open('data/dev.json', 'w') as handle:
        json.dump(dev_data, handle)

    # Since rasa usually reads from markdown files, we are going ti provide the split as MD aswell
    create_rasa_training_set(train_dataframe)
    create_rasa_test_set(eval_dataframe)
    create_rasa_dev_set(dev_dataframe)

    # For later usage, we persist the labels separate from the rest aswell
    with open('data/labels.json', 'w', encoding='utf-8') as file:
        json.dump(train_label_list, file)

    # Add all the labels to the finetuner
    for label in train_label_list:
        textcat.add_label(str(label))

    # Proper represent the labels
    (train_texts, train_cats), (eval_texts, eval_cats) = load_data(
        train_dataframe=train_dataframe, eval_dataframe=eval_dataframe, label_list=train_label_list
    )

    # Configuring the pipeline for the finetuning process
    nlp.add_pipe(textcat, last=True)

    # It might be a good idea to split sentences of an article into separate training samples
    # For the moment, we are skipping that step to keep things simple.

    if sentence_split:
        logger.info(f"Sentencicing ...")
        train_texts, train_cats = make_sentence_examples(nlp, train_texts, train_cats)
        logger.info(f"Extracted {len(train_texts)} training sentences.")

    total_words = sum(len(text.split()) for text in train_texts)
    train_data = list(zip(train_texts, [{"cats": cats} for cats in train_cats]))

    # Initialize the TextCategorizer, and create an optimizer.
    optimizer = nlp.resume_training()
    optimizer.alpha = 0.001
    optimizer.trf_weight_decay = 0.005
    optimizer.L2 = 0.0
    learn_rates = cyclic_triangular_rate(
        learn_rate / 3, learn_rate * 3, 2 * len(train_data) // batch_size
    )

    pbar = tqdm.tqdm(total=100, leave=False)
    results = []
    epoch = 0
    step = 0
    eval_every = 100
    patience = 3
    while True:
        # Train and evaluate
        losses = Counter()
        random.shuffle(train_data)
        batches = minibatch(train_data, size=batch_size)
        for batch in batches:
            optimizer.trf_lr = next(learn_rates)
            texts, annotations = zip(*batch)
            nlp.update(texts, annotations, sgd=optimizer, drop=0.1, losses=losses)
            pbar.update(1)
            if step and (step % eval_every) == 0:
                pbar.close()
                with nlp.use_params(optimizer.averages):
                    scores = evaluate_multiclass(nlp, eval_texts, eval_cats)
                results.append((scores["textcat_acc"], step, epoch))
                print(
                    "{0:.3f}\t{1:.3f}\t{2:.3f}\t{3:.3f}".format(
                        losses["trf_textcat"],
                        scores["textcat_acc"],
                        scores["textcat_cor"],
                        scores["textcat_wrg"],
                    )
                )
                pbar = tqdm.tqdm(total=eval_every, leave=False)
            step += 1
        epoch += 1
        # Stop if no improvement in HP.patience checkpoints
        if results:
            best_score, best_step, best_epoch = max(results)
            if ((step - best_step) // eval_every) >= patience:
                break

    msg = wasabi.Printer()
    table_widths = [2, 4, 6]
    msg.info(f"Best scoring checkpoints")
    msg.row(["Epoch", "Step", "Score"], widths=table_widths)
    msg.row(["-" * width for width in table_widths])
    for score, step, epoch in sorted(results, reverse=True)[:10]:
        msg.row([epoch, step, "%.2f" % (score * 100)], widths=table_widths)

    if output_dir is not None:
        nlp.to_disk(output_dir)
        logger.info("Saved model to {}".format(output_dir))


def make_sentence_examples(nlp, texts, labels):
    """

    :param nlp:
    :param texts:
    :param labels:
    :return:
    """
    sents = []
    sent_cats = []
    for text, cats in zip(texts, labels):
        doc = nlp.make_doc(text)
        doc = nlp.get_pipe("sentencizer")(doc)
        for sent in doc.sents:
            sents.append(sent.text)
            sent_cats.append(cats)
    return sents, sent_cats

def preprocess_text(text):
    """

    :param text:
    :return:
    """

    white_re = re.compile(r"\s\s+")
    text = text.replace("<s>", "<open-s-tag>")
    text = text.replace("</s>", "<close-s-tag>")
    text = white_re.sub(" ", text).strip()
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def load_data(train_dataframe, eval_dataframe, label_list):
    """

    :param train_dataframe:
    :param eval_dataframe:
    :param label_list:
    :return:
    """

    train_data = list(train_dataframe.itertuples(index=False, name=None))
    dev_data = list(eval_dataframe.itertuples(index=False, name=None))
    train_labels, train_texts = _prepare_partition(train_data, preprocess=False, label_list=label_list)
    dev_labels, dev_texts = _prepare_partition(dev_data, preprocess=False, label_list=label_list)
    return (train_labels, train_texts), (dev_labels, dev_texts)


def _prepare_partition(text_label_tuples, *, preprocess=False, label_list):
    """

    :param text_label_tuples:
    :param preprocess:
    :param label_list:
    :return:
    """
    labels, texts = zip(*text_label_tuples)
    if preprocess:
        texts = [preprocess_text(text) for text in texts]
    cats = [{str(i): 1.0 if i == y else 0.0 for i in label_list} for y in labels]

    return texts, cats


def find_max_prob_cat(cats_probs):
    """

    :param cats_probs:
    :return:
    """
    cats, probs = zip(*cats_probs.items())
    idx = np.argmax(probs)
    return cats[idx]


def evaluate_multiclass(nlp, texts, cats):
    """

    :param nlp:
    :param texts:
    :param cats:
    :return:
    """
    correct = 0
    total_words = sum(len(text.split()) for text in texts)
    with tqdm.tqdm(total=total_words, leave=False) as pbar:
        for i, doc in enumerate(nlp.pipe(texts, batch_size=8)):
            true_label = find_max_prob_cat(cats[i])
            pred_label = find_max_prob_cat(doc.cats)
            if true_label == pred_label:
                correct += 1
            pbar.update(len(doc.text.split()))
    return {'textcat_acc': float(correct) / len(texts),
            'textcat_cor': correct,
            'textcat_wrg': len(texts) - correct}


def create_rasa_training_set(df_train_set):
    """

    :param df_train_set:
    :return:
    """
    label_samples = {}
    with open('data/train.md', 'w', encoding='utf-8') as file:
        for index, entry in df_train_set.iterrows():
            if entry['label'] not in label_samples:
                label_samples[entry['label']] = []
                label_samples[entry['label']].append(entry['article'])
            else:
                label_samples[entry['label']].append(entry['article'])
        for label, articles in label_samples.items():
            file.write('## intent:' + label + '\n')
            for article in articles:
                file.write('- ' + article + '\n')


def create_rasa_dev_set(df_dev_set):
    """

    :param df_dev_set:
    :return:
    """
    label_samples = {}
    with open('data/dev.md', 'w', encoding='utf-8') as file:
        for index, entry in df_dev_set.iterrows():
            if entry['label'] not in label_samples:
                label_samples[entry['label']] = []
                label_samples[entry['label']].append(entry['article'])
            else:
                label_samples[entry['label']].append(entry['article'])
        for label, articles in label_samples.items():
            file.write('## intent:' + label + '\n')
            for article in articles:
                file.write('- ' + article + '\n')


def create_rasa_test_set(df_test_set):
    """

    :param df_test_set:
    :return:
    """
    label_samples = {}
    with open('data/test.md', 'w', encoding='utf-8') as file:
        for index, entry in df_test_set.iterrows():
            if entry['label'] not in label_samples:
                label_samples[entry['label']] = []
                label_samples[entry['label']].append(entry['article'])
            else:
                label_samples[entry['label']].append(entry['article'])
        for label, articles in label_samples.items():
            file.write('## intent:' + label + '\n')
            for article in articles:
                file.write('- ' + article + '\n')


if __name__ == "__main__":
    plac.call(main)
