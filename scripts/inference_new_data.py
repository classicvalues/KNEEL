import argparse
import glob
import os
import numpy as np
from kneel.inference import LandmarkAnnotator
from kneel.evaluation import visualize_landmarks
from tqdm import tqdm
import cv2

cv2.ocl.setUseOpenCL(False)
cv2.setNumThreads(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset_path', default='')
    parser.add_argument('--dataset', default='')
    parser.add_argument('--workdir', default='')
    parser.add_argument('--lc_snapshot_path', default='')
    parser.add_argument('--hc_snapshot_path', default='')
    parser.add_argument('--roi_size_mm', type=int, default=140)
    parser.add_argument('--pad', type=int, default=300)
    parser.add_argument('--device',  default='cuda')
    parser.add_argument('--refine', type=bool, default=False)
    parser.add_argument('--visualize', type=bool, default=False)
    parser.add_argument('--mean_std_path', default='')
    args = parser.parse_args()

    global_searcher = LandmarkAnnotator(snapshot_path=args.lc_snapshot_path,
                                        mean_std_path=args.mean_std_path,
                                        device=args.device)

    local_searcher = LandmarkAnnotator(snapshot_path=args.hc_snapshot_path,
                                       mean_std_path=args.mean_std_path,
                                       device=args.device)

    imgs = glob.glob(os.path.join(args.dataset_path, args.dataset, '*'))
    imgs.sort()
    predicted_landmarks = []
    case_names = []
    for img_name in tqdm(imgs, total=len(imgs), desc=f'Annotating..'):
        res = global_searcher.read_dicom(img_name, new_spacing=global_searcher.img_spacing, return_orig=True)
        if len(res) > 0:
            img, orig_spacing, h_orig, w_orig, img_orig = res
        else:
            continue

        # First pass of knee joint center estimation
        roi_size_px = int(args.roi_size_mm * 1. / orig_spacing)
        global_coords = global_searcher.predict_img(img, h_orig, w_orig)
        img_orig = LandmarkAnnotator.pad_img(img_orig, args.pad if args.pad != 0 else None)
        global_coords += args.pad
        h_orig, w_orig = img_orig.shape
        landmarks, right_roi_orig, left_roi_orig = local_searcher.predict_local(img_orig, global_coords,
                                                                                roi_size_px, orig_spacing)

        if args.refine:
            # refinement
            centers_d = np.array([roi_size_px // 2, roi_size_px // 2]) - landmarks[:, 4]
            global_coords -= centers_d
            # prediction for refined centers
            landmarks, right_roi_orig, left_roi_orig = local_searcher.predict_local(img_orig, global_coords,
                                                                                    roi_size_px, orig_spacing)

        if args.visualize:
            visualize_landmarks(right_roi_orig, landmarks[0, :9], landmarks[0, 9:], radius=5)
            visualize_landmarks(left_roi_orig, landmarks[1, :9], landmarks[1, 9:], radius=5)

        landmarks -= args.pad
        landmarks[0, :, :] += global_coords[0, :] - roi_size_px // 2
        landmarks[1, :, :] += global_coords[1, :] - roi_size_px // 2

        predicted_landmarks.append(np.expand_dims(landmarks, 0))
        case_names.append(img_name.split('/')[-1].split('.')[0])

    predicted_landmarks = np.vstack(predicted_landmarks)
    save_dir = os.path.join(args.workdir, args.dataset+'_inference')
    os.makedirs(save_dir, exist_ok=True)
    np.savez(os.path.join(save_dir, f'preds_unpadded{"_refined" if args.refine else ""}.npz'),
             preds=predicted_landmarks,
             imgs=case_names)

