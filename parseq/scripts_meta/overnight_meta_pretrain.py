# encoding: utf-8
"""
A script for running the following zero-shot domain transfer experiments:
* dataset: Overnight
* model: BART encoder + vanilla Transformer decoder for LF
    * lexical token representations are computed based on lexicon
* training: normal (CE on teacher forced target)
"""
import faulthandler
import json
import math
import random
import string
from copy import deepcopy
from functools import partial
from typing import Callable, Set

import fire
# import wandb

import qelos as q   # branch v3
import numpy as np
import torch
from nltk import Tree
from torch.utils.data import DataLoader

from parseq.datasets import OvernightDatasetLoader, pad_and_default_collate, autocollate, Dataset
from parseq.decoding import merge_metric_dicts
from parseq.eval import SeqAccuracies, TreeAccuracy, make_array_of_metrics, CELoss
from parseq.grammar import tree_to_lisp_tokens, lisp_to_tree
from parseq.vocab import SequenceEncoder, Vocab
from transformers import AutoTokenizer, AutoModel, BartConfig, BartModel, BartForConditionalGeneration

UNKID = 3

DATA_RESTORE_REVERSE = False


def get_labels_from_tree(x:Tree):
    ret = {x.label()}
    for child in x:
        ret |= get_labels_from_tree(child)
    return ret


def get_maximum_spanning_examples(examples, mincoverage=1, loadedex=None):
    """
    Sort given examples by the degree they span their vocabulary.
    First examples maximally increase how much least seen tokens are seen.
    :param examples:
    :param mincoverage: the minimum number of times every token must be covered.
     If the token occurs less than 'mincoverage' number of times in given 'examples',
      all examples with that token are included but the 'mincoverage' criterion is not satisfied!
    :return:
    """
    tokencounts = {}
    uniquetokensperexample = []
    examplespertoken = {}        # reverse index from token to example number
    for i, example in enumerate(examples):
        exampletokens = set(get_labels_from_tree(example[1]))
        uniquetokensperexample.append(exampletokens)
        for token in exampletokens:
            if token not in tokencounts:
                tokencounts[token] = 0
            tokencounts[token] += 1
            if token not in examplespertoken:
                examplespertoken[token] = set()
            examplespertoken[token].add(i)

    scorespertoken = {k: len(examples) / len(examplespertoken[k]) for k in examplespertoken.keys()}

    selectiontokencounts = {k: 0 for k, v in tokencounts.items()}

    if loadedex is not None:
        for i, example in enumerate(loadedex):
            exampletokens = set(get_labels_from_tree(example[1]))
            for token in exampletokens:
                if token in selectiontokencounts:
                    selectiontokencounts[token] += 1

    def get_example_score(i):
        minfreq = min(selectiontokencounts.values())
        ret = 0
        for token in uniquetokensperexample[i]:
            ret += 1/8 ** (selectiontokencounts[token] - minfreq)
        return ret

    exampleids = set(range(len(examples)))
    outorder = []

    i = 0

    while len(exampleids) > 0:
        sortedexampleids = sorted(exampleids, key=get_example_score, reverse=True)
        outorder.append(sortedexampleids[0])
        exampleids -= {sortedexampleids[0]}
        # update selection token counts
        for token in uniquetokensperexample[sortedexampleids[0]]:
            selectiontokencounts[token] += 1
        minfreq = np.infty
        for k, v in selectiontokencounts.items():
            if tokencounts[k] < mincoverage and selectiontokencounts[k] >= tokencounts[k]:
                pass
            else:
                minfreq = min(minfreq, selectiontokencounts[k])
        i += 1
        if minfreq >= mincoverage:
            break

    out = [examples[i] for i in outorder]
    print(f"{len(out)}/{len(examples)} examples loaded from domain")
    return out


def get_lf_abstract_transform(examples):
    """
    Receives examples from different domains in the format (_, out_tokens, split, domain).
    Returns a function that transforms a sequence of domain-specific output tokens
        into a sequence of domain-independent tokens, abstracting domain-specific tokens/subtrees.
    :param examples:
    :return:
    """
    # get shared vocabulary
    domainspertoken = {}
    domains = set()
    for i, example in enumerate(examples):
        if "train" in example[2]:
            exampletokens = set(example[1])
            for token in exampletokens:
                if token not in domainspertoken:
                    domainspertoken[token] = set()
                domainspertoken[token].add(example[3])
            domains.add(example[3])

    sharedtokens = set([k for k, v in domainspertoken.items() if len(v) == len(domains)])
    sharedtokens.add("@ABS@")
    sharedtokens.add("@END@")
    sharedtokens.add("@START@")
    sharedtokens.add("@META@")
    sharedtokens.add("@UNK@")
    sharedtokens.add("@PAD@")
    sharedtokens.add("@METARARE@")
    replacement = "@ABS@"

    def example_transform(x):
        abslf = [xe if xe in sharedtokens else replacement for xe in x]
        abslf = ["@ABSSTART"] + abslf[1:]
        return abslf

    return example_transform


def load_ds(traindomains=("restaurants",),
            testdomain="housing",
            min_freq=1,
            mincoverage=1,
            top_k=np.infty,
            batsize=10,
            nl_mode="bert-base-uncased",
            fullsimplify=False,
            add_domain_start=True,
            supportsetting="lex",   # "lex" or "min"
            ):
    """
    :param traindomains:
    :param testdomain:
    :param min_freq:
    :param mincoverage:
    :param top_k:
    :param nl_mode:
    :param fullsimplify:
    :param add_domain_start:
    :param onlyabstract:
    :param pretrainsetting:     "all": use all examples from every domain
                                "lex": use only lexical examples
                                "all+lex": use both
    :param finetunesetting:     "lex": use lexical examples
                                "all": use all training examples
                                "min": use minimal lexicon-covering set of examples
                            ! Test is always over the same original test set.
                            ! Validation is over a fraction of training data
    :return:
    """

    def tokenize_and_add_start(t, _domain, meta=False):
        tokens = tree_to_lisp_tokens(t)
        if not meta:
            starttok = f"@START/{_domain}@" if add_domain_start else "@START@"
            tokens = [starttok] + tokens
        else:
            starttok = f"@META/{_domain}@" if add_domain_start else "@META@"
            tokens = [starttok] + tokens
        return tokens

    domains = {}
    alltrainex = []
    for domain in list(traindomains) + [testdomain]:
        ds = OvernightDatasetLoader(simplify_mode="light" if not fullsimplify else "full", simplify_blocks=True,
                                    restore_reverse=DATA_RESTORE_REVERSE, validfrac=.10)\
            .load(domain=domain)
        domainexamples = [(a, b, c) for a, b, c in ds.examples]
        if supportsetting == "lex":
            domainexamples = [(a, b, "finetune" if c == "lexicon" else c)
                              for a, b, c in domainexamples]
        else:
            domainexamples = [(a, b, c) for a, b, c in domainexamples if c != "lexicon"]
        if domain != testdomain:
            alltrainex += [(a, b, c, domain) for a, b, c in domainexamples if c == "train"]
        domains[domain] = domainexamples

    if supportsetting == "min":
        for domain, domainexamples in domains.items():
            mindomainexamples = get_maximum_spanning_examples([(a, b, c) for a, b, c in domainexamples if c == "train"],
                                          mincoverage=mincoverage,
                                          loadedex=[a for a in alltrainex if a[3] != domain])
            domains[domain] = domains[domain] + [(a, b, "finetune") for a, b, c in mindomainexamples]

    for domain in domains:
        domains[domain] = [(a, tokenize_and_add_start(b, domain, meta=c=="finetune"), c)
                           for a, b, c in domains[domain]]
        # sourceex += ds[(None, None, lambda x: x in ("train", "valid", "lexicon"))].examples       # don't use test examples

    allex = []
    for domain in domains:
        allex += [(a, b, c, domain) for a, b, c in domains[domain]]
    ds = Dataset(allex)

    et = get_lf_abstract_transform(ds[lambda x: x[3] != testdomain].examples)
    ds = ds.map(lambda x: (x[0], x[1], et(x[1]), x[2], x[3]))

    abstracttokens = set()
    abstracttokens.add("@META@")
    abstracttokens.add("@START@")
    abstracttokens.add("@END@")
    abstracttokens.add("@UNK@")
    abstracttokens.add("@PAD@")
    abstracttokens.add("@ABS@")
    abstracttokens.add("@ABSSTART@")
    seqenc_vocab = Vocab(padid=0, startid=1, endid=2, unkid=UNKID)
    seqenc_vocab.add_token("@ABS@", seen=np.infty)
    seqenc_vocab.add_token("@ABSSTART@", seen=np.infty)
    seqenc_vocab.add_token("@METARARE@", seen=np.infty)
    seqenc = SequenceEncoder(vocab=seqenc_vocab, tokenizer=lambda x: x,
                             add_start_token=False, add_end_token=True)
    for example in ds.examples:
        abstracttokens |= set(example[2])
        seqenc.inc_build_vocab(example[1], seen=example[3] in ("train", "finetune") if example[4] != testdomain else example[3] == "finetune")
        seqenc.inc_build_vocab(example[2], seen=example[3] in ("train", "finetune") if example[4] != testdomain else example[3] == "finetune")
    seqenc.finalize_vocab(min_freq=min_freq, top_k=top_k)

    abstracttokenids = {seqenc.vocab[at] for at in abstracttokens}

    nl_tokenizer = AutoTokenizer.from_pretrained(nl_mode)
    def tokenize(x):
        ret = (nl_tokenizer.encode(x[0], return_tensors="pt")[0],
               seqenc.convert(x[1], return_what="tensor"),
               seqenc.convert(x[2], return_what="tensor"),
               x[3],
               x[0], x[1], x[2], x[3])
        return ret

    sourceret = {}
    targetret = {}
    for domain in domains:
        finetuneds = ds[lambda x: x[3] == "finetune" and x[4] == domain].map(tokenize)
        trainds = ds[lambda x: x[3] == "train" and x[4] == domain].map(tokenize)
        validds = ds[lambda x: x[3] == "valid" and x[4] == domain].map(tokenize)
        testds = ds[lambda x: x[3] == "test" and x[4] == domain].map(tokenize)
        if domain == testdomain:
            ret = targetret
        else:
            ret = sourceret
        ret[domain] = {
            "finetune":DataLoader(finetuneds, batch_size=batsize, shuffle=True, collate_fn=partial(autocollate, pad_value=0)),
            "train": DataLoader(trainds, batch_size=batsize, shuffle=True, collate_fn=partial(autocollate, pad_value=0)),
            "valid": DataLoader(validds, batch_size=batsize, shuffle=False, collate_fn=partial(autocollate, pad_value=0)),
            "test": DataLoader(testds, batch_size=batsize, shuffle=False, collate_fn=partial(autocollate, pad_value=0))
        }
    return sourceret, targetret, nl_tokenizer, seqenc, abstracttokenids


class BartGenerator(BartForConditionalGeneration):
    def __init__(self, config:BartConfig, emb=None, outlin=None):
        super(BartGenerator, self).__init__(config)
        if emb is not None:
            self.model.shared = emb
            self.model.decoder.embed_tokens = emb
        if outlin is not None:
            self.outlin = outlin
        else:
            self.outlin = torch.nn.Linear(config.d_model, config.vocab_size)

    def forward(
        self,
        input_ids,
        attention_mask=None,
        encoder_outputs=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        decoder_cached_states=None,
        use_cache=False,
        **unused
    ):
        outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            encoder_outputs=encoder_outputs,
            decoder_attention_mask=decoder_attention_mask,
            decoder_cached_states=decoder_cached_states,
            use_cache=use_cache,
        )
        lm_logits = self.outlin(outputs[0])
        outputs = (lm_logits,) + outputs[1:]  # Add hidden states and attention if they are here
        return outputs


class BartGeneratorTrain(torch.nn.Module):
    def __init__(self, model:BartGenerator, smoothing=0., tensor2tree:Callable=None, orderless:Set[str]=set(),
                 maxlen:int=100, numbeam:int=1, **kw):
        super(BartGeneratorTrain, self).__init__(**kw)
        self.model = model

        # CE loss
        self.ce = CELoss(ignore_index=model.config.pad_token_id, smoothing=smoothing)

        # accuracies
        self.accs = SeqAccuracies()
        self.accs.padid = model.config.pad_token_id
        self.accs.unkid = UNKID

        self.tensor2tree = tensor2tree
        self.orderless = orderless
        self.maxlen, self.numbeam = maxlen, numbeam
        self.treeacc = TreeAccuracy(tensor2tree=tensor2tree,
                                    orderless=orderless)

        self.metrics = [self.ce, self.accs, self.treeacc]

    def forward(self, input_ids, output_ids, *args, **kwargs):
        ret = self.model(input_ids, attention_mask=input_ids!=self.model.config.pad_token_id, decoder_input_ids=output_ids)
        probs = ret[0]
        _, predactions = probs.max(-1)
        outputs = [metric(probs, predactions, output_ids[:, 1:]) for metric in self.metrics]
        outputs = merge_metric_dicts(*outputs)
        return outputs, ret

    def get_test_model(self, maxlen:int=None, numbeam:int=None):
        maxlen = self.maxlen if maxlen is None else maxlen
        numbeam = self.numbeam if numbeam is None else numbeam
        ret = BartGeneratorTest(self.model, maxlen=maxlen, numbeam=numbeam,
                                tensor2tree=self.tensor2tree, orderless=self.orderless)
        return ret


class AbstractBartGeneratorTrain(torch.nn.Module):
    def __init__(self, model:BartGenerator, smoothing=0., tensor2tree:Callable=None, orderless:Set[str]=set(), tokenmask=None, **kw):
        super(AbstractBartGeneratorTrain, self).__init__(**kw)
        self.model = model

        # CE loss
        self.ce = CELoss(ignore_index=model.config.pad_token_id, smoothing=smoothing)

        # accuracies
        self.accs = SeqAccuracies()
        self.accs.padid = model.config.pad_token_id
        self.accs.unkid = UNKID

        self.treeacc = TreeAccuracy(tensor2tree=tensor2tree,
                                    orderless=orderless)

        self.register_buffer("tokenmask", tokenmask)

        self.metrics = [self.ce, self.accs, self.treeacc]

    def forward(self, input_ids, _, output_ids, *args, **kwargs):
        ret = self.model(input_ids, attention_mask=input_ids!=self.model.config.pad_token_id, decoder_input_ids=output_ids)
        probs = ret[0]  # (batsize, seqlen, vocsize)
        if self.tokenmask is not None:  # (vocsize,)
            probs += torch.log(self.tokenmask[None, None, :])
        _, predactions = probs.max(-1)
        outputs = [metric(probs, predactions, output_ids[:, 1:]) for metric in self.metrics]
        outputs = merge_metric_dicts(*outputs)
        return outputs, ret


class BartGeneratorTest(BartGeneratorTrain):
    def __init__(self, model:BartGenerator, maxlen:int=5, numbeam:int=None,
                 tensor2tree:Callable=None, orderless:Set[str]=set(), **kw):
        super(BartGeneratorTest, self).__init__(model, **kw)
        self.maxlen, self.numbeam = maxlen, numbeam
        # accuracies
        self.accs = SeqAccuracies()
        self.accs.padid = model.config.pad_token_id
        self.accs.unkid = UNKID

        self.treeacc = TreeAccuracy(tensor2tree=tensor2tree,
                                    orderless=orderless)

        self.metrics = [self.accs, self.treeacc]

    def forward(self, input_ids, output_ids, *args, **kwargs):
        ret = self.model.generate(input_ids,
                                  decoder_input_ids=output_ids[:, 0:1],
                                  attention_mask=input_ids!=self.model.config.pad_token_id,
                                  max_length=self.maxlen,
                                  num_beams=self.numbeam)
        outputs = [metric(None, ret[:, 1:], output_ids[:, 1:]) for metric in self.metrics]
        outputs = merge_metric_dicts(*outputs)
        return outputs, ret


class SpecialEmbedding(torch.nn.Embedding):
    def __init__(self, num_embeddings, embedding_dim, padding_idx=None,
                 metarare_source=None, metarare_targets=None):
        super(SpecialEmbedding, self).__init__(num_embeddings, embedding_dim, padding_idx=padding_idx)
        self.metarare_source = metarare_source
        self.register_buffer("metarare_targets", metarare_targets)
        # self.metarare = self.weight[self.metarare_source, :]
        # self.base_emb = torch.nn.Embedding(num_embeddings, embedding_dim, padding_idx)
        self.extra_emb = torch.nn.Embedding(num_embeddings, embedding_dim, padding_idx)
        self.extra_emb.weight.data.fill_(0)

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        # metarare_targets are 1 for domain-specific tokens
        base_emb = super(SpecialEmbedding, self).forward(input)
        metarare_emb = super(SpecialEmbedding, self).forward(torch.ones_like(input) * self.metarare_source)
        extra_emb = self.extra_emb(input)
        switch = self.metarare_targets[input]
        emb = switch[:, :, None] * (extra_emb + metarare_emb) \
              + (1 - switch[:, :, None]) * base_emb
        return emb


class SpecialOutlin(torch.nn.Linear):
    def __init__(self, dim, vocsize, metarare_source=None, metarare_targets=None, bias=True):
        super(SpecialOutlin, self).__init__(dim, vocsize, bias=bias)
        self.metarare_source = metarare_source
        self.register_buffer("metarare_targets", metarare_targets)
        # self.metarare = self.weight[self.metarare_source, :]
        # self.base_emb = torch.nn.Embedding(num_embeddings, embedding_dim, padding_idx)
        self.extra_lin = torch.nn.Linear(dim, vocsize, bias=bias)
        self.extra_lin.weight.data.fill_(0)
        self.extra_lin.bias.data.fill_(0)

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        base_logits = super(SpecialOutlin, self).forward(input)
        extra_logits = self.extra_lin(input)
        metarare_vector = self.weight[self.metarare_source, :]
        metarare_bias = self.bias[self.metarare_source]
        if input.dim() == 2:
            switch = self.metarare_targets[None, :]
            metarare_logits = torch.einsum("bd,d->b", input, metarare_vector) + metarare_bias
        else:
            switch = self.metarare_targets[None, None, :]
            metarare_logits = torch.einsum("bsd,d->bs", input, metarare_vector) + metarare_bias

        logits = switch * (extra_logits + metarare_logits[:, :, None]) + (1 - switch) * base_logits
        return logits


def create_model(encoder_name="bert-base-uncased",
                 dec_vocabsize=None, dec_layers=6, dec_dim=640, dec_heads=8, dropout=0.,
                 maxlen=20, smoothing=0., numbeam=1, tensor2tree=None,
                 abstract_token_ids=set(), abs_id=None,
                 dometarare=True):
    if encoder_name != "bert-base-uncased":
        raise NotImplementedError(f"encoder '{encoder_name}' not supported yet.")
    pretrained = AutoModel.from_pretrained(encoder_name)
    encoder = pretrained

    class BertEncoderWrapper(torch.nn.Module):
        def __init__(self, model, dropout=0., **kw):
            super(BertEncoderWrapper, self).__init__(**kw)
            self.model = model
            self.proj = torch.nn.Linear(pretrained.config.hidden_size, dec_dim, bias=False)
            self.dropout = torch.nn.Dropout(dropout)

        def forward(self, input_ids, attention_mask=None):
            ret, _ = self.model(input_ids, attention_mask=attention_mask)
            if pretrained.config.hidden_size != dec_dim:
                ret = self.proj(ret)
            ret = self.dropout(ret)
            ret = (ret, None, None)
            return ret

    encoder = BertEncoderWrapper(encoder, dropout=dropout)

    decoder_config = BartConfig(d_model=dec_dim,
                                pad_token_id=0,
                                bos_token_id=1,
                                vocab_size=dec_vocabsize,
                                decoder_attention_heads=dec_heads//2,
                                decoder_layers=dec_layers,
                                dropout=dropout,
                                attention_dropout=min(0.1, dropout/2),
                                decoder_ffn_dim=dec_dim*4,
                                encoder_attention_heads=dec_heads,
                                encoder_layers=dec_layers,
                                encoder_ffn_dim=dec_dim*4,
                                )

    isabstracttokenmask = torch.zeros(dec_vocabsize)
    for abstract_token_id in abstract_token_ids:
        isabstracttokenmask[abstract_token_id] = 1

    # create special embeddings and output layer
    if dometarare:
        emb = SpecialEmbedding(decoder_config.vocab_size,
                               decoder_config.d_model,
                               decoder_config.pad_token_id,
                               metarare_source=abs_id,
                               metarare_targets=1-isabstracttokenmask)
        outlin = SpecialOutlin(decoder_config.d_model,
                               decoder_config.vocab_size,
                               metarare_source=abs_id,
                               metarare_targets=1-isabstracttokenmask)
    else:
        emb, outlin = None, None

    model = BartGenerator(decoder_config, emb, outlin)
    model.model.encoder = encoder

    orderless = {"op:and", "SW:concat"}

    trainmodel = BartGeneratorTrain(model, smoothing=smoothing, tensor2tree=tensor2tree, orderless=orderless,
                                    maxlen=maxlen, numbeam=numbeam)
    abstracttrainmodel = AbstractBartGeneratorTrain(model, smoothing=smoothing, tensor2tree=tensor2tree, orderless=orderless,
                                                    tokenmask=isabstracttokenmask)
    # testmodel = BartGeneratorTest(model, maxlen=maxlen, numbeam=numbeam, tensor2tree=tensor2tree, orderless=orderless)
    return trainmodel, abstracttrainmodel


def _tensor2tree(x, D:Vocab=None):
    # x: 1D int tensor
    x = list(x.detach().cpu().numpy())
    x = [D(xe) for xe in x]
    x = [xe for xe in x if xe != D.padtoken]

    # find first @END@ and cut off
    parentheses_balance = 0
    for i in range(len(x)):
        if x[i] ==D.endtoken:
            x = x[:i]
            break
        elif x[i] == "(" or x[i][-1] == "(":
            parentheses_balance += 1
        elif x[i] == ")":
            parentheses_balance -= 1
        else:
            pass

    # balance parentheses
    while parentheses_balance > 0:
        x.append(")")
        parentheses_balance -= 1
    i = len(x) - 1
    while parentheses_balance < 0 and i > 0:
        if x[i] == ")":
            x.pop(i)
            parentheses_balance += 1
        i -= 1

    # convert to nltk.Tree
    try:
        tree, parsestate = lisp_to_tree(" ".join(x), None)
    except Exception as e:
        tree = None
    return tree


def move_grad(source=None, target=None):
    source_params = {k: v for k, v in source.named_parameters()}
    for k, v in target.named_parameters():
        assert(v.size() == source_params[k].size())
        if v.grad is None:
            v.grad = source_params[k].grad
        else:
            v.grad += source_params[k].grad
    source.zero_grad()


def reset_special_grads_inner(_m):
    if isinstance(_m.model.model.decoder.embed_tokens, SpecialEmbedding):
        _m.model.model.decoder.embed_tokens.weight.grad = None
    if isinstance(_m.model.outlin, SpecialOutlin):
        _m.model.outlin.weight.grad = None
        _m.model.outlin.bias.grad = None


def reset_special_grads_outer(_m):
    if isinstance(_m.model.model.decoder.embed_tokens, SpecialEmbedding):
        _m.model.model.decoder.embed_tokens.extra_emb.weight.grad = None
    if isinstance(_m.model.outlin, SpecialOutlin):
        _m.model.outlin.extra_lin.weight.grad = None
        _m.model.outlin.extra_lin.bias.grad = None


def infiter(a):
    while True:
        for ae in a:
            yield ae


def meta_train_epoch(model=None,
                     absmodel=None,
                     data=None,
                     optim=None,
                     get_ft_model=None,
                     get_ft_optim=None,
                     losses=None,
                     abslosses=None,
                     ftlosses=None,
                     device=torch.device("cpu"),
                     tt=q.ticktock(" -"),
                current_epoch=0,
                     max_epochs=0,
                     finetunesteps=1,
                     on_start=tuple(),
                     on_end=tuple(),
                print_every_batch=False,
                     clipgradnorm=None,
                     gradacc=1,
                     abstract_contrib=0.):
    """
    Performs an epoch of training on given model, with data from given dataloader, using given optimizer,
    with loss computed based on given losses.
    :param model:
    :param data: dictionary from domains to dicts of dataloaders
    :param optim:
    :param losses:  list of loss wrappers
    :param device:  device to put batches on
    :param tt:
    :param current_epoch:
    :param max_epochs:
    :param _train_batch:    train batch function, default is train_batch
    :param on_start:
    :param on_end:
    :return:
    """
    for loss in losses:
        loss.push_epoch_to_history(epoch=current_epoch-1)
        loss.reset_agg()
        loss.loss.to(device)

    model.to(device)
    absmodel.to(device)

    [e() for e in on_start]

    q.epoch_reset(model)
    optim.zero_grad()
    numbatsperdomain = {k: len(data[k]["train"]) for k in data}
    totalnumtrainbats = sum(numbatsperdomain.values())
    probbatsperdomain = {k: numbatsperdomain[k] / totalnumtrainbats for k in numbatsperdomain}

    # iter-ize training dataloaders in data
    for k, v in data.items():
        v["train"] = iter(v["train"])

    outerstep_i = 0
    while True:
        outerbatch = None
        exhausted_domains = set()
        while outerbatch is None and len(exhausted_domains) < len(data):
            ks, vs = zip(*probbatsperdomain.items())
            chosendomain = np.random.choice(ks, p=vs)
            try:
                outerbatch = next(data[chosendomain]["train"])
            except StopIteration as e:
                print(f"stopping iteration - outerstep_i: {outerstep_i}")
                exhausted_domains.add(chosendomain)
                outerbatch = None

        if outerbatch is None:
            break

        # perform K number of inner steps
        ftmodel = get_ft_model(model)
        ftoptim = get_ft_optim(ftmodel)
        inneriter = infiter(data[chosendomain]["finetune"])

        oldemb = ftmodel.model.model.decoder.embed_tokens.weight + 0
        oldlin = ftmodel.model.outlin.weight + 0

        for loss in ftlosses:
            loss.push_epoch_to_history(epoch=str(current_epoch - 1)+"."+chosendomain)
            loss.reset_agg()
            loss.loss.to(device)

        for innerstep_i in range(finetunesteps):
            innerbatch = next(inneriter)
            ttmsg = q.train_batch(batch=innerbatch, model=ftmodel, optim=ftoptim, losses=ftlosses, device=device,
                                  batch_number=innerstep_i, max_batches=finetunesteps, current_epoch=current_epoch,
                                  max_epochs=max_epochs,
                                  on_before_optim_step=[
                                      partial(clipgradnorm, _m=ftmodel),
                                      partial(reset_special_grads_inner, _m=ftmodel)])
            if print_every_batch:
                tt.msg(ttmsg)
            else:
                tt.live(ttmsg)
        # after K inner updates
        # perform outer update on main model weights
        # do outer update:
        #   1. obtain gradient on inner-updated model using outerbatch,
        #   2. apply gradient on main model weights
        ttmsg = q.train_batch(batch=outerbatch, model=ftmodel, optim=None, losses=losses, device=device,
                             batch_number=outerstep_i, max_batches=totalnumtrainbats, current_epoch=current_epoch,
                             max_epochs=max_epochs, gradient_accumulation_steps=gradacc)
                            # , on_before_optim_step=[
                            #     partial(clipgradnorm, _m=model),
                            #     partial(copy_grad, source=ftmodel, target=model)])
        move_grad(ftmodel, model)
        reset_special_grads_outer(model)

        # do abstract prediction
        abs_ttmsg = q.train_batch(batch=outerbatch, model=absmodel, optim=None, losses=abslosses, device=device,
                                  batch_number=outerstep_i, max_batches=totalnumtrainbats, current_epoch=current_epoch,
                                  max_epochs=max_epochs, gradient_accumulation_steps=gradacc,
                                  loss_scale=abstract_contrib)

        # do optim step
        _do_optim_step = ((outerstep_i+1) % gradacc) == 0
        _do_optim_step = _do_optim_step or (outerstep_i+1) == totalnumtrainbats  # force optim step at the end of epoch
        if _do_optim_step:
            optim.step()
            optim.zero_grad()

        if print_every_batch:
            tt.msg(ttmsg + " -- " + abs_ttmsg)
        else:
            tt.live(ttmsg + " -- " + abs_ttmsg)

        outerstep_i += 1
    tt.stoplive()
    [e() for e in on_end]
    ttmsg = q.pp_epoch_losses(*losses) + " -- " + q.pp_epoch_losses(*abslosses)
    return ttmsg



def meta_test_epoch(model=None,
                    data=None,
                     get_ft_model=None,
                     get_ft_optim=None,
                    losses=None,
                    ftlosses=None,
                    finetunesteps=1,
                    bestfinetunestepsvar=None,
                    bestfinetunestepswhichmetric=None,
                    bestfinetunelowerisbetter=False,
                    evalinterval=-1,
                    device=torch.device("cpu"),
                    clipgradnorm=None,
            current_epoch=0, max_epochs=0, print_every_batch=False,
            on_start=tuple(), on_start_batch=tuple(), on_end_batch=tuple(), on_end=tuple(),
                    on_outer_start=tuple(), on_outer_end=tuple()):
    """
    Performs a test epoch. If run=True, runs, otherwise returns partially filled function.
    :param model:
    :param dataloader:
    :param losses:
    :param device:
    :param current_epoch:
    :param max_epochs:
    :param on_start:
    :param on_start_batch:
    :param on_end_batch:
    :param on_end:
    :return:
    """
    tt = q.ticktock(" -")
    model.to(device)
    q.epoch_reset(model)
    [e() for e in on_outer_start]

    lossesperdomain = {}

    for domain in data:
        lossesperdomain[domain] = []
        # doing one domain
        domaindata = data[domain]
        # perform fine-tuning (with early stopping if valid is given
        ftmodel = get_ft_model(model)
        ftoptim = get_ft_optim(ftmodel)
        ftmodel.train()
        inneriter = infiter(domaindata["finetune"])

        for loss in ftlosses:
            loss.push_epoch_to_history(epoch=str(current_epoch - 1)+"."+domain)
            loss.reset_agg()
            loss.loss.to(device)

        for innerstep_i in range(finetunesteps):
            innerbatch = next(inneriter)
            ttmsg = q.train_batch(batch=innerbatch, model=ftmodel, optim=ftoptim, losses=ftlosses, device=device,
                                  batch_number=innerstep_i, max_batches=finetunesteps, current_epoch=current_epoch, max_epochs=max_epochs,
                                  on_before_optim_step=[partial(clipgradnorm, _m=ftmodel),
                                                        partial(reset_special_grads_inner, _m=ftmodel)])
            if print_every_batch:
                tt.msg(ttmsg)
            else:
                tt.live(ttmsg)

            if (evalinterval >= 0 and (innerstep_i+1) % evalinterval == 0) or \
               (evalinterval < 0 and innerstep_i+1 == finetunesteps):
                _losses = deepcopy(losses)
                q.test_epoch(ftmodel, dataloader=domaindata["valid"], losses=_losses, device=device,
                             current_epoch=current_epoch, max_epochs=max_epochs, print_every_batch=print_every_batch,
                             on_start=on_start, on_end=on_end, on_start_batch=on_start_batch, on_end_batch=on_end_batch)
                lossesperdomain[domain].append(_losses)

    # find best number of steps
    if evalinterval >= 0:
        metricsmatrix = np.zeros((len(lossesperdomain), math.ceil(finetunesteps / evalinterval), len(losses)))
        for i, domain in enumerate(sorted(lossesperdomain.keys())):
            for j, steplosses in enumerate(lossesperdomain[domain]):
                for k, lossval in enumerate(steplosses):
                    metricsmatrix[i, j, k] = lossval.get_epoch_error()
        metricsmatrix = metricsmatrix.mean(0)   # (numevals, numlosses)
        critvals = metricsmatrix[:, bestfinetunestepswhichmetric]   # (numevals)
        critvals = critvals * (1 if bestfinetunelowerisbetter is False else -1)
        bestfinetunestepsvar.v = np.argmax(critvals)
        k = q.v(bestfinetunestepsvar)
    else:
        k = 0

    for loss, _loss in zip(losses, metricsmatrix[k, :]):
        loss.epoch_agg_values.append(_loss)
        loss.epoch_agg_sizes.append(1)
    tt.stoplive()
    [e() for e in on_outer_end]
    ttmsg = q.pp_epoch_losses(*losses) + f" [@{1 + (k * evalinterval if evalinterval >= 0 else finetunesteps)}]"
    return ttmsg


def run(traindomains="blocks+recipes", #"ALL",
        domain="restaurants",
        mincoverage=2,
        lr=0.0001,
        enclrmul=0.1,
        numbeam=1,
        ftlr=0.0001,
        cosinelr=False,
        warmup=0.,
        batsize=30,
        epochs=100,
        finetunesteps=5,
        maxfinetunesteps=4,
        evalinterval=2,
        pretrainepochs=100,
        dropout=0.1,
        wreg=1e-9,
        gradnorm=3,
        gradacc=1,
        smoothing=0.,
        patience=5,
        gpu=-1,
        seed=123456789,
        encoder="bert-base-uncased",
        numlayers=6,
        hdim=600,
        numheads=8,
        maxlen=30,
        fullsimplify=True,
        domainstart=False,
        supportsetting="lex",   # "lex" or "min"
        dometarare=True,
        abscontrib=1.,
        ):
    settings = locals().copy()
    print(json.dumps(settings, indent=4))
    # wandb.init(project=f"overnight_joint_pretrain_fewshot_{pretrainsetting}-{finetunesetting}-{domain}",
    #            reinit=True, config=settings)
    if traindomains == "ALL":
        alldomains = {"recipes", "restaurants", "blocks", "calendar", "housing", "publications"}
        traindomains = alldomains - {domain, }
    else:
        traindomains = set(traindomains.split("+"))
    random.seed(seed)
    torch.manual_seed(seed)
    np.random.seed(seed)
    tt = q.ticktock("script")
    device = torch.device("cpu") if gpu < 0 else torch.device(gpu)

    tt.tick("loading data")
    sourcedss, targetdss, nltok, flenc, abstract_token_ids = \
        load_ds(traindomains=traindomains, testdomain=domain, nl_mode=encoder, mincoverage=mincoverage,
                fullsimplify=fullsimplify, add_domain_start=domainstart, batsize=batsize,
                supportsetting=supportsetting)
    tt.tock("data loaded")

    tt.tick("creating model")
    trainm, abstrainm = create_model(encoder_name=encoder,
                                 dec_vocabsize=flenc.vocab.number_of_ids(),
                                 dec_layers=numlayers,
                                 dec_dim=hdim,
                                 dec_heads=numheads,
                                 dropout=dropout,
                                 smoothing=smoothing,
                                 maxlen=maxlen,
                                 numbeam=numbeam,
                                 tensor2tree=partial(_tensor2tree, D=flenc.vocab),
                                 abstract_token_ids=abstract_token_ids,
                                 abs_id=flenc.vocab["@METARARE@"],
                                 dometarare=dometarare,
                                 )
    tt.tock("model created")

    # region pretrain on all domains
    metrics = make_array_of_metrics("loss", "elem_acc", "seq_acc", "tree_acc")
    absmetrics = make_array_of_metrics("loss", "tree_acc")
    ftmetrics = make_array_of_metrics("loss", "elem_acc", "seq_acc", "tree_acc")
    vmetrics = make_array_of_metrics("seq_acc", "tree_acc")
    vftmetrics = make_array_of_metrics("loss", "elem_acc", "seq_acc", "tree_acc")
    xmetrics = make_array_of_metrics("seq_acc", "tree_acc")
    xftmetrics = make_array_of_metrics("loss", "elem_acc", "seq_acc", "tree_acc")

    # region parameters
    def get_parameters(m, _lr, _enclrmul):
        trainable_params = list(m.named_parameters())

        # tt.msg("different param groups")
        encparams = [v for k, v in trainable_params if k.startswith("model.model.encoder")]
        otherparams = [v for k, v in trainable_params if not k.startswith("model.model.encoder")]
        if len(encparams) == 0:
            raise Exception("No encoder parameters found!")
        paramgroups = [{"params": encparams, "lr": _lr * _enclrmul},
                       {"params": otherparams}]
        return paramgroups
    # endregion

    def get_optim(_m, _lr, _enclrmul, _wreg=0):
        paramgroups = get_parameters(_m, _lr=lr, _enclrmul=enclrmul)
        optim = torch.optim.Adam(paramgroups, lr=lr, weight_decay=wreg)
        return optim

    def clipgradnorm(_m, _norm):
        torch.nn.utils.clip_grad_norm_(_m.parameters(), _norm)

    eyt = q.EarlyStopper(vmetrics[1], patience=patience, min_epochs=10, more_is_better=True, remember_f=lambda: deepcopy(trainm.model))
    # def wandb_logger():
    #     d = {}
    #     for name, loss in zip(["loss", "elem_acc", "seq_acc", "tree_acc"], metrics):
    #         d["train_"+name] = loss.get_epoch_error()
    #     for name, loss in zip(["seq_acc", "tree_acc"], vmetrics):
    #         d["valid_"+name] = loss.get_epoch_error()
    #     wandb.log(d)
    t_max = epochs
    optim = get_optim(trainm, lr, enclrmul, wreg)
    print(f"Total number of updates: {t_max} .")
    if cosinelr:
        lr_schedule = q.sched.Linear(steps=warmup) >> q.sched.Cosine(steps=t_max-warmup) >> 0.
    else:
        lr_schedule = q.sched.Linear(steps=warmup) >> 1.
    lr_schedule = q.sched.LRSchedule(optim, lr_schedule)

    trainepoch = partial(meta_train_epoch,
                         model=trainm,
                         absmodel=abstrainm,
                         data=sourcedss,
                         optim=optim,
                         get_ft_model=lambda x: deepcopy(x),
                         get_ft_optim=partial(get_optim,
                                              _lr=ftlr,
                                              _enclrmul=enclrmul,
                                              _wreg=wreg),
                         losses=metrics,
                         abslosses=absmetrics,
                         ftlosses=ftmetrics,
                         finetunesteps=finetunesteps,
                         clipgradnorm=partial(clipgradnorm, _norm=gradnorm),
                         device=device,
                         on_end=[lambda: lr_schedule.step()],
                         gradacc=gradacc,
                         abstract_contrib=abscontrib,)

    bestfinetunesteps = q.hyperparam(-1)
    testepoch = partial(meta_test_epoch,
                        model=trainm,
                        data=sourcedss,
                        get_ft_model=lambda x: deepcopy(x),
                        get_ft_optim=partial(get_optim,
                                             _lr=ftlr,
                                             _enclrmul=enclrmul,
                                             _wreg=wreg),
                        bestfinetunestepsvar=bestfinetunesteps,
                        bestfinetunestepswhichmetric=1,
                        losses=vmetrics,
                        ftlosses=vftmetrics,
                        finetunesteps=maxfinetunesteps,
                        evalinterval=evalinterval,
                        clipgradnorm=partial(clipgradnorm, _norm=gradnorm),
                        device=device,
                        print_every_batch=False)

    print(testepoch())

    q.run_training(run_train_epoch=trainepoch,
                   run_valid_epoch=testepoch,
                   max_epochs=pretrainepochs)

    validepoch = partial(q.test_epoch, model=testm, dataloader=vdl, losses=vmetrics, device=device,
                         on_end=[lambda: eyt.on_epoch_end()])#, lambda: wandb_logger()])

    tt.tick("pretraining")
    q.run_training(run_train_epoch=trainepoch, run_valid_epoch=validepoch, max_epochs=pretrainepochs,
                   check_stop=[lambda: eyt.check_stop()])
    tt.tock("done pretraining")

    if eyt.get_remembered() is not None:
        tt.msg("reloaded")
        trainm.model = eyt.get_remembered()

    # endregion

    # region finetune
    ftmetrics = make_array_of_metrics("loss", "elem_acc", "seq_acc", "tree_acc")
    ftvmetrics = make_array_of_metrics("seq_acc", "tree_acc")
    ftxmetrics = make_array_of_metrics("seq_acc", "tree_acc")

    trainable_params = list(trainm.named_parameters())
    exclude_params = set()
    # exclude_params.add("model.model.inp_emb.emb.weight")  # don't train input embeddings if doing glove
    if len(exclude_params) > 0:
        trainable_params = [(k, v) for k, v in trainable_params if k not in exclude_params]

    tt.msg("different param groups")
    encparams = [v for k, v in trainable_params if k.startswith("model.model.encoder")]
    otherparams = [v for k, v in trainable_params if not k.startswith("model.model.encoder")]
    if len(encparams) == 0:
        raise Exception("No encoder parameters found!")
    paramgroups = [{"params": encparams, "lr": ftlr * enclrmul},
                   {"params": otherparams}]

    ftoptim = torch.optim.Adam(paramgroups, lr=ftlr, weight_decay=wreg)

    clipgradnorm = lambda: torch.nn.utils.clip_grad_norm_(trainm.parameters(), gradnorm)

    eyt = q.EarlyStopper(ftvmetrics[1], patience=1000, min_epochs=10, more_is_better=True,
                         remember_f=lambda: deepcopy(trainm.model))

    # def wandb_logger_ft():
    #     d = {}
    #     for name, loss in zip(["loss", "elem_acc", "seq_acc", "tree_acc"], ftmetrics):
    #         d["ft_train_" + name] = loss.get_epoch_error()
    #     for name, loss in zip(["seq_acc", "tree_acc"], ftvmetrics):
    #         d["ft_valid_" + name] = loss.get_epoch_error()
    #     wandb.log(d)

    t_max = epochs
    print(f"Total number of updates: {t_max} .")
    if cosinelr:
        lr_schedule = q.sched.Linear(steps=warmup) >> q.sched.Cosine(steps=t_max - warmup) >> 0.
    else:
        lr_schedule = q.sched.Linear(steps=warmup) >> 1.
    lr_schedule = q.sched.LRSchedule(ftoptim, lr_schedule)

    trainbatch = partial(q.train_batch, on_before_optim_step=[clipgradnorm])
    trainepoch = partial(q.train_epoch, model=trainm, dataloader=ftdl, optim=ftoptim, losses=ftmetrics,
                         _train_batch=trainbatch, device=device, on_end=[lambda: lr_schedule.step()])
    validepoch = partial(q.test_epoch, model=testm, dataloader=fvdl, losses=ftvmetrics, device=device,
                         on_end=[lambda: eyt.on_epoch_end()])#, lambda: wandb_logger_ft()])

    tt.tick("training")
    q.run_training(run_train_epoch=trainepoch, run_valid_epoch=validepoch, max_epochs=epochs,
                   check_stop=[lambda: eyt.check_stop()])
    tt.tock("done training")

    if eyt.get_remembered() is not None:
        tt.msg("reloaded")
        trainm.model = eyt.get_remembered()
        testm.model = eyt.get_remembered()

    # endregion

    tt.tick("testing")
    validresults = q.test_epoch(model=testm, dataloader=fvdl, losses=ftvmetrics, device=device)
    testresults = q.test_epoch(model=testm, dataloader=xdl, losses=ftxmetrics, device=device)
    print(validresults)
    print(testresults)
    tt.tock("tested")
    # settings.update({"train_seqacc": losses[]})

    for metricarray, datasplit in zip([ftmetrics, ftvmetrics, ftxmetrics], ["train", "valid", "test"]):
        for metric in metricarray:
            settings[f"{datasplit}_{metric.name}"] = metric.get_epoch_error()

    # wandb.config.update(settings)
    # print(settings)
    return settings


def run_experiments(domain="restaurants", gpu=-1, patience=10, cosinelr=False, mincoverage=2, fullsimplify=True, uselexicon=False):
    ranges = {
        "lr": [0.0001, 0.00001], #[0.001, 0.0001, 0.00001],
        "ftlr": [0.00003],
        "enclrmul": [1., 0.1], #[1., 0.1, 0.01],
        "warmup": [2],
        "epochs": [100], #[50, 100],
        "pretrainepochs": [100],
        "numheads": [8, 12, 16],
        "numlayers": [3, 6, 9],
        "dropout": [.1],
        "hdim": [768, 960], #[192, 384, 768, 960],
        "seed": [12345678], #, 98387670, 23655798, 66453829],      # TODO: add more later
    }
    p = __file__ + f".{domain}"
    def check_config(x):
        effectiveenclr = x["enclrmul"] * x["lr"]
        if effectiveenclr < 0.00001:
            return False
        dimperhead = x["hdim"] / x["numheads"]
        if dimperhead < 20 or dimperhead > 100:
            return False
        return True

    q.run_experiments(run, ranges, path_prefix=p, check_config=check_config,
                      domain=domain, fullsimplify=fullsimplify, uselexicon=uselexicon,
                      gpu=gpu, patience=patience, cosinelr=cosinelr, mincoverage=mincoverage)


def run_experiments_seed(domain="restaurants", gpu=-1, patience=10, cosinelr=False, fullsimplify=True, batsize=50,
                         smoothing=0.2, dropout=.1, numlayers=3, numheads=12, hdim=768, domainstart=False, pretrainbatsize=100,
                         nopretrain=False, numbeam=1, onlyabstract=False, supportsetting="lex"):
    ranges = {
        "lr": [0.0001],
        "ftlr": [0.0001],
        "enclrmul": [0.1],
        "warmup": [2],
        "epochs": [100],
        "pretrainepochs": [100],
        "numheads": [numheads],
        "numlayers": [numlayers],
        "dropout": [dropout],
        "smoothing": [smoothing],
        "hdim": [hdim],
        "numbeam": [numbeam],
        "batsize": [batsize],
        "seed": [12345678, 65748390, 98387670, 23655798, 66453829],     # TODO: add more later
    }
    p = __file__ + f".{domain}"
    def check_config(x):
        effectiveenclr = x["enclrmul"] * x["lr"]
        if effectiveenclr < 0.000005:
            return False
        dimperhead = x["hdim"] / x["numheads"]
        if dimperhead < 20 or dimperhead > 100:
            return False
        return True

    q.run_experiments(run, ranges, path_prefix=p, check_config=check_config,
                      domain=domain, fullsimplify=fullsimplify,
                      gpu=gpu, patience=patience, cosinelr=cosinelr,
                      domainstart=domainstart, pretrainbatsize=pretrainbatsize,
                      supportsetting=supportsetting,
                      maxfinetunesteps=30, evalinterval=5,
                      nopretrain=nopretrain)



if __name__ == '__main__':
    faulthandler.enable()
    ret = q.argprun(run)
    # print(ret)
    # q.argprun(run_experiments)
    fire.Fire(run_experiments_seed)