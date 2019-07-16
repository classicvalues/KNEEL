import cv2
import torch
import glob
import os
import pickle
import numpy as np

from functools import partial
from torchvision import transforms as tvt
import solt.core as slc
import solt.transforms as slt
import solt.data as sld

from deeppipeline.common.transforms import apply_by_index
from deeppipeline.common.normalization import normalize_channel_wise

from kneel.model import init_model_from_args
from kneel.data.utils import read_dicom, process_xray, convert_img


def wrap_slt(img, annotator_type='lc'):
    if annotator_type == 'lc':
        img = np.dstack((img, img, img))
        row, col, _ = img.shape
        # right, left encoding
        img = (img[:, :col // 2 + col % 2], img[:, col // 2:])
    else:
        img_right = np.dstack((img[0], img[0], img[0]))
        img_left = np.dstack((img[1], img[1], img[1]))
        img = (img_right, img_left)

    return sld.DataContainer((img[0], img[1]), 'II')


def unwrap_slt(dc, norm_trf):
    return torch.stack(norm_trf(list(map(convert_img, dc.data))))


class NFoldInferenceModel(torch.nn.Module):
    def __init__(self, models):
        super(NFoldInferenceModel, self).__init__()
        modules = dict()
        for idx, m in enumerate(models):
            modules[f'model_{idx+1}'] = m
        self.n_models = len(models)
        self.__dict__['_modules'] = modules

    def forward(self, x):
        res = 0
        for model_id in range(1, self.n_models+1):
            res += getattr(self, f'model_{model_id}')(x)
        return res / self.n_models


class LandmarkAnnotator(object):
    def __init__(self, snapshot_path, mean_std_path, device='cpu'):
        self.fold_snapshots = glob.glob(os.path.join(snapshot_path, 'fold_*.pth'))
        models = []
        self.device = device
        with open(os.path.join(snapshot_path, 'session.pkl'), 'rb') as f:
            snapshot_session = pickle.load(f)

        snp_args = snapshot_session['args'][0]

        for snp_name in self.fold_snapshots:
            net = init_model_from_args(snp_args)
            snp = torch.load(snp_name)['model']
            net.load_state_dict(snp)
            models.append(net)
        dummy = torch.FloatTensor(2, 3, snp_args.crop_x, snp_args.crop_y).to(device=self.device)
        self.net = NFoldInferenceModel(models).to(self.device)
        self.net.eval()

        with torch.no_grad():
            self.net = torch.jit.trace(self.net, dummy)
        mean_vector, std_vector = np.load(mean_std_path)

        self.annotator_type = snp_args.annotations
        self.img_spacing = getattr(snp_args, f'{snp_args.annotations}_spacing')

        norm_trf = partial(normalize_channel_wise, mean=mean_vector, std=std_vector)
        norm_trf = partial(apply_by_index, transform=norm_trf, idx=[0, 1])

        self.trf = tvt.Compose([
            partial(wrap_slt, annotator_type=self.annotator_type),
            slc.Stream([
                slt.PadTransform((snp_args.pad_x, snp_args.pad_y), padding='z'),
                slt.CropTransform((snp_args.crop_x, snp_args.crop_y), crop_mode='c'),
            ]),
            partial(unwrap_slt, norm_trf=norm_trf),
        ])

    @staticmethod
    def pad_img(img, pad):
        if pad is not None:
            row, col = img.shape
            tmp = np.zeros((row + 2 * pad, col + 2 * pad))
            tmp[pad:pad + row, pad:pad + col] = img
            return tmp
        else:
            return img

    @staticmethod
    def read_dicom(img_path, new_spacing, return_orig=False, pad_img=None):
        res = read_dicom(img_path)
        if res is None:
            return []
        img_orig, orig_spacing, _ = res
        img_orig = process_xray(img_orig).astype(np.uint8)
        img_orig = LandmarkAnnotator.pad_img(img_orig, pad_img)

        h_orig, w_orig = img_orig.shape

        img = LandmarkAnnotator.resize_to_spacing(img_orig, orig_spacing, new_spacing)

        if return_orig:
            return img, orig_spacing, h_orig, w_orig, img_orig
        return img, orig_spacing, h_orig, w_orig

    @staticmethod
    def resize_to_spacing(img, spacing, new_spacing):
        scale = spacing / new_spacing
        return cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)))

    def predict_img(self, img, h_orig=None, w_orig=None, rounded=True):
        img_batch = self.trf(img)
        res = self.batch_inference(img_batch).squeeze()

        if self.annotator_type == 'lc':
            res = self.handle_lc_out(res, h_orig, w_orig)
        else:
            res = self.handle_hc_out(res, h_orig, w_orig)
        if rounded:
            return np.round(res).astype(int)
        return res

    @staticmethod
    def handle_hc_out(res, h_orig, w_orig):
        res[:, :, 0] = w_orig * res[:, :, 0]
        res[:, :, 1] = h_orig * res[:, :, 1]
        return res

    @staticmethod
    def handle_lc_out(res, h_orig, w_orig):
        # right preds
        res[0, 0] = (w_orig // 2 + w_orig % 2) * res[0, 0]
        res[0, 1] = h_orig * res[0, 1]

        # left preds
        res[1, 0] = w_orig // 2 + w_orig // 2 * res[1, 0]
        res[1, 1] = h_orig * res[1, 1]

        return res

    @staticmethod
    def localize_left_right_rois(img, roi_size_pix, coords):
        s = roi_size_pix // 2

        roi_right = img[coords[0, 1] - s:coords[0, 1] + s,
                        coords[0, 0] - s:coords[0, 0] + s]

        roi_left = img[coords[1, 1] - s:coords[1, 1] + s,
                       coords[1, 0] - s:coords[1, 0] + s]

        return roi_right, roi_left

    def batch_inference(self, batch: torch.tensor):
        if batch.device != self.device:
            batch = batch.to(self.device)
            with torch.no_grad():
                res = self.net(batch)
        return res.to('cpu').numpy()

    def predict_local(self, img, center_coords, roi_size_px, orig_spacing):
        if self.annotator_type != 'hc':
            raise ValueError('This method can be called only for local search model')
        right_roi_orig, left_roi_orig = LandmarkAnnotator.localize_left_right_rois(img, roi_size_px, center_coords)

        left_roi_orig = left_roi_orig[:, ::-1]
        try:
            right_roi = LandmarkAnnotator.resize_to_spacing(right_roi_orig, orig_spacing, self.img_spacing)
        except cv2.error:
            right_roi = None

        try:
            left_roi = LandmarkAnnotator.resize_to_spacing(left_roi_orig, orig_spacing, self.img_spacing)
        except cv2.error:
            left_roi = None

        if left_roi is None and right_roi is None:
            return None, None, None
        elif left_roi is None:
            landmarks = self.predict_img((right_roi, right_roi.copy()),
                                         h_orig=roi_size_px,
                                         w_orig=roi_size_px)
            landmarks[1] = np.nan
            left_roi_orig = None
        elif right_roi is None:
            landmarks = self.predict_img((left_roi.copy(), left_roi),
                                         h_orig=roi_size_px,
                                         w_orig=roi_size_px)
            landmarks[0] = np.nan
            right_roi_orig = None
        else:
            landmarks = self.predict_img((right_roi, left_roi),
                                         h_orig=roi_size_px,
                                         w_orig=roi_size_px)

            left_roi_orig = left_roi_orig[:, ::-1]
            landmarks[1, :, 0] = roi_size_px - landmarks[1, :, 0]

        return landmarks, right_roi_orig, left_roi_orig
