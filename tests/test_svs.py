import numpy as np
import pysinsy
import pytest
from nnmnkwii.io import hts
from nnsvs.io.hts import full_to_mono
from nnsvs.pretrained import retrieve_pretrained_model
from nnsvs.svs import NEUTRINO, SPSVS
from nnsvs.util import example_xml_file


@pytest.mark.parametrize("segmented_synthesis", [False, True])
@pytest.mark.parametrize("post_filter_type", ["merlin", "gv", "nnsvs"])
@pytest.mark.parametrize("vocoder_type", ["world"])
def test_svs(segmented_synthesis, post_filter_type, vocoder_type):
    model_dir = retrieve_pretrained_model("r9y9/yoko_latest")
    engine = SPSVS(model_dir)

    contexts = pysinsy.extract_fullcontext(example_xml_file(key="get_over"))
    labels = hts.HTSLabelFile.create_from_contexts(contexts)

    wav, sr = engine.svs(
        labels,
        segmented_synthesis=segmented_synthesis,
        post_filter_type=post_filter_type,
        vocoder_type=vocoder_type,
    )
    assert sr == 48000
    assert np.isfinite(wav).all()


def test_neutrino():
    model_dir = retrieve_pretrained_model("r9y9/yoko_latest")
    engine = NEUTRINO(model_dir)

    musicxml = example_xml_file(key="get_over")
    full_labels, mono_labels = engine.musicxml2label(musicxml)

    assert len(full_labels) == len(mono_labels)

    num_phrases = engine.get_num_phrases(mono_labels)
    assert num_phrases >= 1

    timing_labels = full_to_mono(engine.predict_timing(full_labels))
    f0, mgc, bap = engine.predict_acoustic(full_labels, timing_labels)
    assert len(f0) == len(mgc) == len(bap)

    wav = engine.predict_waveform(f0, mgc, bap, vocoder_type="world")
    assert np.isfinite(wav).all()


def test_neutrino_phrase():
    model_dir = retrieve_pretrained_model("r9y9/yoko_latest")
    engine = NEUTRINO(model_dir)

    musicxml = example_xml_file(key="get_over")
    full_labels, mono_labels = engine.musicxml2label(musicxml)
    num_phrases = engine.get_num_phrases(mono_labels)
    assert num_phrases >= 1

    timing_labels = full_to_mono(engine.predict_timing(full_labels))

    phraselist = engine.get_phraselist(full_labels, timing_labels)
    print(phraselist)

    f0, mgc, bap = engine.predict_acoustic(full_labels, timing_labels, phrase_num=1)
    assert len(f0) == len(mgc) == len(bap)

    wav = engine.predict_waveform(f0, mgc, bap, vocoder_type="world")
    assert np.isfinite(wav).all()
