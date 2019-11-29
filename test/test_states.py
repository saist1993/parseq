from unittest import TestCase
import numpy as np
import torch

from parseq.states import State, BasicDecoderState
from parseq.vocab import SentenceEncoder


class Test_State(TestCase):
    def test_state_create(self):
        x = State()
        x.set(k=torch.randn(3, 4))
        xsub = State()
        xsub.set(v=torch.rand(3, 2))
        x.set(s=xsub)
        x.set(l=["sqdf", "qdsf", "qdsf"])
        print(x)
        print(x._schema_keys)
        print(x.k)
        print(x.s.v)
        print(x.l)

    def test_state_merge(self):
        x = State()
        x.set(k=torch.randn(3, 4))
        xsub = State()
        xsub.set(v=torch.rand(3, 2))
        x.set(s=xsub)
        x.set(l=["sqdf", "qdsf", "qdsf"])
        y = State()
        y.set(k=torch.randn(2, 4))
        ysub = State()
        ysub.set(v=torch.rand(2, 2))
        y.set(s=ysub)
        y.set(l=["sqdf", "qdsf"])
        z = State.merge([x, y])
        print(z._schema_keys)
        print(z.k)
        print(x.k)
        print(y.k)
        print(z.s.v)
        print(z.l)

    def test_state_getitem(self):
        x = State()
        x.set(k=torch.randn(5, 4))
        xsub = State()
        xsub.set(v=torch.rand(5, 2))
        x.set(s=xsub)
        x.set(l=["sqdf", "qdsf", "qdsf", "qf", "qsd"])
        y = x[2]
        print(x[2].k)
        print(x[2].l)
        print(x[2].s.v)
        print(x[:2].k)
        print(x[:2].l)
        print(x[:2].s.v)

    def test_state_setitem(self):
        x = State()
        x.set(k=torch.randn(5, 4))
        xsub = State()
        xsub.set(v=torch.rand(5, 2))
        x.set(s=xsub)
        x.set(l=["sqdf", "qdsf", "qdsf", "a", "b"])
        y = State()
        y.set(k=torch.ones(2, 4))
        ysub = State()
        ysub.set(v=torch.ones(2, 2))
        y.set(s=ysub)
        y.set(l=["o", "o"])

        x[1:3] = y
        print(x.k)
        print(x.s.v)
        print(x.l)

    def test_slicing_by_indexes(self):
        x = State(k=torch.rand(6, 3), s="a b c d e f".split())
        print(x.k)
        print(x[[1, 2]].k)
        print(x[[1, 2]].s)

        print(x[np.asarray([1, 2])].k)
        print(x[np.asarray([1, 2])].s)

        print(x[torch.tensor([1, 2])].k)
        print(x[torch.tensor([1, 2])].s)

    def test_seting_by_indexes(self):
        x = State(k=torch.rand(6, 3), s="a b c d e f".split())
        y = State(k=torch.zeros(2, 3), s="o o".split())

        x[[1, 3]] = y
        print(x.k)
        print(x.s)

        x = State(k=torch.rand(6, 3), s="a b c d e f".split())
        y = State(k=torch.zeros(2, 3), s="o o".split())

        x[np.asarray([1, 3])] = y
        print(x.k)
        print(x.s)

        x = State(k=torch.rand(6, 3), s="a b c d e f".split())
        y = State(k=torch.zeros(2, 3), s="o o".split())

        x[torch.tensor([1, 3])] = y
        print(x.k)
        print(x.s)

    def test_copy_non_detached(self):
        x = State(k=torch.nn.Parameter(torch.rand(5, 3)))
        y = x.make_copy(detach=False)
        l = y.k.sum()
        l.backward()
        print(x.k.grad)

    def test_copy_detached(self):
        x = State(k=torch.nn.Parameter(torch.rand(5, 3)))
        y = x.make_copy()
        y.k[:] = 0
        print(x.k)
        print(y.k)

    def test_copy_deep(self):
        x = State(k=["a", "b", "c"])
        y = x.make_copy(deep=False)
        y.k[:] = "q"
        print(x.k)
        print(y.k)


class TestBasicDecoderState(TestCase):
    def test_create(self):
        se = SentenceEncoder(tokenizer=lambda x: x.split())
        texts = ["i went to chocolate", "awesome is @PAD@ @PAD@", "the meaning of life"]
        for t in texts:
            se.inc_build_vocab(t)
        se.finalize_vocab()
        x = [BasicDecoderState([t], [t], se, se) for t in texts]
        merged_x = x[0].merge(x)
        texts = ["i went to chocolate", "awesome is", "the meaning of life"]
        batch_x = BasicDecoderState(texts, texts, se, se)
        print(merged_x.inp_tensor)
        print(batch_x.inp_tensor)
        self.assertTrue(torch.allclose(merged_x.inp_tensor, batch_x.inp_tensor))
        self.assertTrue(torch.allclose(merged_x.gold_tensor, batch_x.gold_tensor))

    def test_decoder_API(self):
        texts = ["i went to chocolate", "awesome is", "the meaning of life"]
        se = SentenceEncoder(tokenizer=lambda x: x.split())
        for t in texts:
            se.inc_build_vocab(t)
        se.finalize_vocab()
        x = BasicDecoderState(texts, texts, se, se)
        print(x.inp_tensor)
        print("terminated")
        print(x.is_terminated())
        print(x.all_terminated())
        print("prev_actions")
        x.start_decoding()
        print(x.prev_actions)
        print("step")
        x.step(["i", torch.tensor([7]), "the"])
        print(x.prev_actions)
        print(x.followed_actions)

