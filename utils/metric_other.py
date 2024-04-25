import numpy as np
from torchvision.models import vgg16
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from PIL import Image
# from saliency_det import get_mask_np
from torchvision import transforms
import json
from PIL import ImageDraw
import cv2
from tqdm import tqdm
import time
import glob
import os
import matplotlib.pyplot as plt
from utils.util import box_cxcywh_to_xyxy
from einops import rearrange, reduce, repeat

device = "cuda" if torch.cuda.is_available() else "cpu"

color = ['blue', 'green', 'red', 'yellow', 'black']

# 一样
def cal_R_und(test_annotation):
    underlay_over = []
    for layout in test_annotation:
        for lay in layout:
            if lay['category_id'] == 3:  # For underlay
                max_over = 0
                for l in layout:
                    if l['category_id'] != 3:  # For all elements in other categories
                        x1 = l['bbox'][0]
                        x2 = l['bbox'][0] + l['bbox'][2]
                        y1 = l['bbox'][1]
                        y2 = l['bbox'][1] + l['bbox'][3]
                        x3 = lay['bbox'][0]
                        x4 = lay['bbox'][0] + lay['bbox'][2]
                        y3 = lay['bbox'][1]
                        y4 = lay['bbox'][1] + lay['bbox'][3]
                        x_over = max(min(x2, x4) - max(x1, x3), 0)
                        y_over = max(min(y2, y4) - max(y1, y3), 0)
                        over = x_over * y_over / (l['bbox'][2] * l['bbox'][3])
                        max_over = max(max_over, over)

                underlay_over.append(max_over)
    return sum(underlay_over) / len(underlay_over) if len(underlay_over) != 0 else 0


def Rcom_rdam(img_names, clses, boxes):
    def nn_conv2d(im, sobel_kernel):
        conv_op = nn.Conv2d(1, 1, 3, bias=False)
        sobel_kernel = sobel_kernel.reshape((1, 1, 3, 3))
        conv_op.weight.data = torch.from_numpy(sobel_kernel)
        gradient = conv_op(Variable(im))
        return gradient

    sobel_x = np.array([[-1, 0, 1],
                        [-2, 0, 2],
                        [-1, 0, 1]], dtype='float32')
    sobel_y = np.array([[-1, -2, -1],
                        [0, 0, 0],
                        [1, 2, 1]], dtype='float32')

    R_coms = []
    for idx, name in enumerate(img_names):
        gray = Image.open(os.path.join("Dataset/test/image_canvas", name)).convert("L").resize((513, 750))
        cls = np.array(clses[idx].cpu(), dtype=int)
        box = np.array(boxes[idx].cpu(), dtype=int)
        for c, b in zip(cls, box):
            if c == 1:
                x1, y1, x2, y2 = b
                gray = gray.crop((x1, y1, x2, y2))
                gray_array = np.array(gray, dtype='float32')
                gray_tensor = torch.from_numpy(gray_array.reshape((1, 1, gray_array.shape[0], gray_array.shape[1])))

                try:  # The w or h can be 0
                    image_x = nn_conv2d(gray_tensor, sobel_x)
                    image_y = nn_conv2d(gray_tensor, sobel_y)
                    image_xy = torch.mean(torch.sqrt(image_x ** 2 + image_y ** 2)).detach().numpy()
                    R_coms.append(image_xy)
                except:
                    continue

    return sum(R_coms) / len(R_coms) if len(R_coms) != 0 else 0


def compute_alignment_ralf(img_size, clses, boxes):
    """
    Computes some alignment metrics that are different to each other in previous works.
    Lower values are generally better.
    Attribute-conditioned Layout GAN for Automatic Graphic Design (TVCG2020)
    https://arxiv.org/abs/2009.05284
    """
    w, h = img_size
    bbox = boxes.permute(2, 0, 1)
    mask = (clses != 0)
    mask = mask.squeeze(-1)
    _, S = mask.size()

    xl, yt, xr, yb = bbox
    xl, xr = xl / w, xr / w
    yt, yb = yt / h, yb / h
    xc, yc = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    X = torch.stack([xl, xc, xr, yt, yc, yb], dim=1)

    X = X.unsqueeze(-1) - X.unsqueeze(-2)
    idx = torch.arange(X.size(2), device=X.device)
    X[:, :, idx, idx] = 1.0
    X = X.abs().permute(0, 2, 1, 3)
    X[~mask] = 1.0
    X = X.min(-1).values.min(-1).values
    X.masked_fill_(X.eq(1.0), 0.0)
    X = -torch.log10(1 - X)

    # original
    # return X.sum(-1) / mask.float().sum(-1)

    score = reduce(X, "b s -> b", reduction="sum")
    score_normalized = score / reduce(mask, "b s -> b", reduction="sum")
    score_normalized[torch.isnan(score_normalized)] = 0.0

    Y = torch.stack([xl, xc, xr], dim=1)
    Y = rearrange(Y, "b x s -> b x 1 s") - rearrange(Y, "b x s -> b x s 1")

    batch_mask = rearrange(~mask, "b s -> b 1 s") | rearrange(~mask, "b s -> b s 1")
    idx = torch.arange(S, device=Y.device)
    batch_mask[:, idx, idx] = True
    batch_mask = repeat(batch_mask, "b s1 s2 -> b x s1 s2", x=3)
    Y[batch_mask] = 1.0

    # Y = rearrange(Y.abs(), "b x s1 s2 -> b s1 x s2")
    # Y = reduce(Y, "b x s1 s2 -> b x", "min")
    # Y = rearrange(Y.abs(), " -> b s1 x s2")
    Y = reduce(Y.abs(), "b x s1 s2 -> b s1", "min")
    Y[Y == 1.0] = 0.0
    score_Y = reduce(Y, "b s -> b", "sum")

    results = {
        "alignment-ACLayoutGAN": score.mean(),  # Because it may be confusing.
        "alignment-LayoutGAN++": score_normalized.mean(),
        "alignment-NDN": score_Y.mean(),  # Because it may be confusing.
    }
    # return {k: v.tolist() for (k, v) in results.items()}
    return results["alignment-LayoutGAN++"]