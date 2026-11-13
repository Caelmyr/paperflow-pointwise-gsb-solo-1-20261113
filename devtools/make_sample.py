"""生成用于笔迹粗细调整开发与测试的合成扫描页（可信测试夹具，非用户数据）。"""
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = '/workspace/resources/NotoSansCJK-Regular.ttc'


def main(path):
    rng = np.random.default_rng(7)
    h, w = 900, 1200
    base = np.zeros((h, w, 3), np.uint8)
    base[:] = (232, 238, 246)  # BGR 米黄纸底
    grad = np.tile(np.linspace(0, 18, w, dtype=np.float32), (h, 1))
    grad += np.tile(np.linspace(0, 10, h, dtype=np.float32)[:, None], (1, w))
    base = np.clip(base.astype(np.float32) - grad[:, :, None], 0, 255)
    page = np.clip(base + rng.normal(0, 3.2, (h, w, 1)), 0, 255).astype(np.uint8)

    pil = Image.fromarray(cv2.cvtColor(page, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font_title = ImageFont.truetype(FONT, 52)
    font_mid = ImageFont.truetype(FONT, 34)
    font_small = ImageFont.truetype(FONT, 26)
    faint = (150, 150, 150)
    darker = (110, 110, 110)
    warm = (120, 130, 140)

    draw.text((55, 55), 'PaperFlow 汉字形态测试 圆圈回门口', font=font_title, fill=faint)
    draw.text((55, 135), '细小的标点：，。、；：（）【】“”！？', font=font_mid, fill=faint)
    draw.text((55, 195), 'adjacent strokes: 八 人 入 川 朋 林 弱', font=font_mid, fill=darker)
    draw.text((55, 255), '深浅混排 ABCDEFG abcdefg 0123456789', font=font_mid, fill=faint)
    draw.text((55, 330), '横竖撇捺折提点 封口断笔连字检查', font=font_mid, fill=faint)
    draw.text((55, 720), '第二段正文：保留原笔迹颜色与轮廓', font=font_mid, fill=warm)
    draw.text((55, 780), '局部边界同一笔画不得切成两种粗细', font=font_mid, fill=warm)
    page = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)

    # 彩色插图（不得被改写）
    cv2.rectangle(page, (760, 90), (1140, 360), (180, 120, 60), -1)
    cv2.circle(page, (950, 225), 90, (70, 160, 90), -1)
    cv2.ellipse(page, (1080, 300), (60, 35), 30, 0, 360, (60, 80, 200), -1)
    cv2.putText(page, 'COLOR PLATE', (790, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)

    # 红色印章（不得被改写）
    cv2.circle(page, (1050, 520), 95, (60, 60, 210), 4)
    cv2.circle(page, (1050, 520), 78, (60, 60, 210), 1)
    cv2.line(page, (980, 520), (1120, 520), (60, 60, 210), 2)
    cv2.line(page, (1050, 450), (1050, 590), (60, 60, 210), 2)
    cv2.putText(page, 'SEAL', (1012, 530), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 210), 2, cv2.LINE_AA)

    # 细线表格（不应被加粗）
    for x in range(60, 700, 90):
        cv2.line(page, (x, 430), (x, 680), (90, 90, 90), 1)
    for y in range(430, 681, 50):
        cv2.line(page, (60, y), (690, y), (90, 90, 90), 1)

    cv2.imwrite(path, page)
    print(path, page.shape)


if __name__ == '__main__':
    main(sys.argv[1])
