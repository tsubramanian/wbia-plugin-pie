from __future__ import absolute_import, division, print_function
from ibeis.control import controller_inject
from ibeis.constants import ANNOTATION_TABLE
import utool as ut
import dtool as dt
import numpy as np
import os
import json


(print, rrr, profile) = ut.inject2(__name__)

_, register_ibs_method = controller_inject.make_ibs_register_decorator(__name__)
register_api = controller_inject.get_ibeis_flask_api(__name__)
register_preproc_annot = controller_inject.register_preprocs['annot']

_PLUGIN_FOLDER  = os.path.dirname(os.path.realpath(__file__))
_DEFAULT_CONFIG = os.path.join(_PLUGIN_FOLDER, 'configs/manta.json')


# note: an embedding is 256xfloat8, aka 2kb in size (using default config)
@register_ibs_method
def pie_embedding(ibs, aid_list, config_path=_DEFAULT_CONFIG, use_depc=True):
    if use_depc:
        config = {'config_path': config_path}
        embeddings = ibs.depc_annot.get("PieEmbedding", aid_list, 'embedding', config=config)
    else:
        embeddings = pie_compute_embedding(ibs, aid_list, config_path=config_path)
    return embeddings


class PieEmbeddingConfig(dt.Config):  # NOQA
    _param_info_list = [
        ut.ParamInfo('config_path', _DEFAULT_CONFIG),
    ]


@register_preproc_annot(
    tablename='PieEmbedding', parents=[ANNOTATION_TABLE],
    colnames=['embedding'], coltypes=[np.ndarray],
    configclass=PieEmbeddingConfig,
    fname='pie',
    chunksize=128)
@register_ibs_method
def pie_embedding_depc(depc, aid_list, config=_DEFAULT_CONFIG):
    ibs = depc.controller
    embs = pie_compute_embedding(ibs, aid_list, config)
    for aid, emb in zip(aid_list, embs):
        yield (np.array(emb), )


# TODO: delete the generated files in dbpath when we're done computing embeddings
def pie_compute_embedding(ibs, aid_list, config_path=_DEFAULT_CONFIG, output_dir=None, prefix=None, export=False):
    dbpath = ibs.pie_preprocess(aid_list)
    from .compute_db import compute
    embeddings = compute(dbpath, config_path, output_dir, prefix, export)
    return embeddings


# This function calls the PIE image preprocessing method preproc_db.preproc,
# whose input is a folder full of pre-cropped images. preproc then resizes all
# images to dimensions specified in the config file and organizes the resized
# images in sub-folders for each label (name). These folders will be read by
# PIE's embedding-compute function later.
@register_ibs_method
def pie_preprocess(ibs, aid_list, config_path=_DEFAULT_CONFIG):
    output_dir = pie_preproc_dir(aid_list, config_path)
    label_file_path = os.path.join(output_dir, 'name_map.csv')
    label_file = ibs.pie_name_csv(aid_list, fpath=label_file_path)
    img_path = ibs.imgdir
    from .preproc_db import preproc
    dbpath = preproc(img_path, config_path, lfile=label_file, output=output_dir)
    return dbpath


# pie's preprocess works on every image in a folder, so we put 'em in a folder
def pie_preproc_dir(aid_list, config_path=_DEFAULT_CONFIG):
    # hash the aid_list to make a unique folder name
    unique_prefix = str(hash(tuple(aid_list)))
    with open(config_path) as config_file:
        config = json.loads(config_file.read())
    conf_output_dir = config['prod']['output']
    output_dir = os.path.join(_PLUGIN_FOLDER, conf_output_dir, unique_prefix)
    if not os.path.isdir(output_dir):
        print("PIE preproc_dir creating output directory %s" % output_dir)
        os.makedirs(output_dir)
    print('PIE preproc_dir for aids %s returning %s' % (aid_list, output_dir))
    return output_dir


# PIE's preproc and embed funcs require a .csv file linking filnames to labels (names)
@register_ibs_method
def pie_name_csv(ibs, aid_list, fpath="/home/wildme/code/ibeis-pie-module/ibeis_pie/examples/dev/name_map.csv"):
    name_texts = ibs.get_annot_name_texts(aid_list)
    fnames = ibs.get_annot_image_paths(aid_list)
    # only want final, file part of fpaths
    fnames = [fname.split('/')[-1] for fname in fnames]
    csv_dicts = [{'file': f, 'label': l} for (f, l) in zip(fnames, name_texts)]
    _write_csv_dicts(csv_dicts, fpath)
    print("Saved PIE name file to %s" % fpath)
    return fpath


def _write_csv_dicts(csv_dicts, fpath):
    import csv
    keys = csv_dicts[0].keys()
    with open(fpath, 'w') as output_file:
        dict_writer = csv.DictWriter(output_file, keys)
        dict_writer.writeheader()
        dict_writer.writerows(csv_dicts)


@register_ibs_method
def pie_predict_light(ibs, qaid, daid_list, config_path=_DEFAULT_CONFIG):
    # just call embeddings once bc of significant startup time on PIE's bulk embedding-generator
    all_aids  = daid_list + [qaid]
    all_embs  = ibs.pie_embedding(all_aids)

    # now get the embeddings into the shape and type PIE expects
    db_embs   = np.array(all_embs[:-1])
    query_emb = np.array(all_embs[-1:])  # query_emb.shape = (1, 256)
    db_labels = np.array(ibs.get_annot_name_texts(daid_list))

    from .predict import pred_light
    ans = pred_light(query_emb, db_embs, db_labels, config_path)
    return ans


# PIE's predict.predict function is more of a command-line program than a package function. It works off of .csv files, .json files, and images stored in hierachical folders, and does all sorts of preprocessing assuming the qaid embedding hasn't been computed before (so ofc, it computes that). This method is basically deprecated and was a proof-of-concept/MVP, and pie_predict_light should be used in practice. Kept around so we can verify that pie_predict_light produces the same results as the released predict function.
@register_ibs_method
def pie_predict(ibs, qaid, daid_list, config_path=_DEFAULT_CONFIG, display=False):
    config = ibs.pie_predict_prepare_config(daid_list, _DEFAULT_CONFIG)
    impath = ibs.get_annot_image_paths(qaid)

    from .predict import predict
    ans = predict(impath, config, config_path, display)
    return ans


# This func modifies a base PIE config file, which contains network parameters as well as database and image paths, keeping the network parameters but updating db/image paths to correspond to daid_list
@register_ibs_method
def pie_predict_prepare_config(ibs, daid_list, base_config_file=_DEFAULT_CONFIG):

    pred_data_dir = ibs.pie_ensure_predict_datafiles(daid_list)

    with open(base_config_file) as conf_file:
        config = json.loads(conf_file.read())

    config['prod']['embeddings'] = pred_data_dir
    config['prod']['temp']       = pred_data_dir

    return config


# pie_predict requires embeddings stored in a .csv file that is identified in the config file, as well as a file linking image names to labels (names). This func makes and saves those csvs if necessary, in a unique folder for a given daid_list.
@register_ibs_method
def pie_ensure_predict_datafiles(ibs, daid_list, base_config_file=_DEFAULT_CONFIG):

    pred_data_dir = pie_annot_info_dir(daid_list)
    embs_fname = os.path.join(pred_data_dir, '_emb.csv')
    lbls_fname = os.path.join(pred_data_dir, '_lbl.csv')

    if not os.path.isfile(embs_fname):
        embeddings = ibs.pie_embedding(daid_list)
        embs_fname = _write_embeddings_csv(embeddings, embs_fname)
    if not os.path.isfile(lbls_fname):
        lbls_fname = _write_labels_csv(ibs, daid_list, lbls_fname)

    return pred_data_dir


# directory where we'll store embeddings and label .csv's to be read by PIE
def pie_annot_info_dir(aid_list):
    embeddings_dir  = os.path.join(_PLUGIN_FOLDER, 'embeddings')
    unique_label    = str(hash(tuple(aid_list)))
    output_dir = os.path.join(embeddings_dir, unique_label)
    if not os.path.isdir(output_dir):
        print("PIE embeddings_dir creating output directory %s" % output_dir)
        os.makedirs(output_dir)
    return output_dir


def _write_embeddings_csv(embeddings, fname):
    ncols = len(embeddings[0])
    header = ['emb_' + str(i) for i in range(ncols)]
    header = ','.join(header)
    np.savetxt(fname, embeddings, delimiter=',', newline='\n', header=header)
    print('PIE wrote embeddings csv to %s' % fname)
    return fname


def _write_labels_csv(ibs, aid_list, fname):
    names = ibs.get_annot_name_texts(aid_list)
    # PIE expects a zero-indexed "class" column that corresponds with the names; like temporary nids
    unique_names = list(set(names))
    name_class_dict = {name: i for (name, i) in zip(unique_names, range(len(unique_names)))}
    classes = [name_class_dict[name] for name in names]
    files   = ibs.get_annot_image_paths(aid_list)
    csv_dicts = [{'class': c, 'file': f, 'name': n} for (c, f, n) in zip(classes, files, names)]
    _write_csv_dicts(csv_dicts, fname)
    print('PIE wrote labels csv to %s' % fname)
    return fname


# quality-control method to compare the dicts from both predict methods
# IDK if it's randomness in k-means or float errors, but the distances differ in practice by ~2e-8
@register_ibs_method
def _pie_compare_dicts(ibs, answer_dict1, answer_dict2, dist_tolerance=1e-5):

    labels1 = [entry['label'] for entry in answer_dict1]
    labels2 = [entry['label'] for entry in answer_dict2]
    agree = [lab1 == lab2 for (lab1, lab2) in zip(labels1, labels2)]
    assert(all(agree), "Label rankings differ at rank %s" % agree.index(False))
    print("Labels agree")

    distances1 = [entry['distance'] for entry in answer_dict1]
    distances2 = [entry['distance'] for entry in answer_dict2]
    diffs = [abs(d1 - d2) for (d1, d2) in zip(distances1, distances2)]
    assert(max(diffs) < dist_tolerance,
           "Distances diverge at rank %s" % diffs.index(max(diffs)))
    print("Distances are all within tolerance of %s" % dist_tolerance)


@register_ibs_method
def pie_identify(ibs, aid_list):
    # TODO
    return


if __name__ == '__main__':
    r"""
    CommandLine:
        python -m ibeis_deepsense._plugin --allexamples
    """
    import multiprocessing
    multiprocessing.freeze_support()  # for win32
    import utool as ut  # NOQA
    ut.doctest_funcs()
