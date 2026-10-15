import os
import re
import random
from PIL import Image, ImageEnhance

class AntiSpamProcessor:
    """
    Bộ tiền xử lý giúp mỗi Fanpage trong 50 trang nhận được
    1 phiên bản Văn bản và Hình ảnh có chữ ký số (MD5/Hash) hoàn toàn khác nhau.
    """

    @staticmethod
    def spin_text(text: str) -> str:
        pattern = re.compile(r'\{([^{}]*)\}')
        while True:
            match = pattern.search(text)
            if not match:
                break
            options = match.group(1).split('|')
            replacement = random.choice(options)
            text = text[:match.start()] + replacement + text[match.end():]
        
        icons = ["✨", "🔥", "❤️", "🛍️", "🌸", "💫", "🏷️"]
        return f"{text}\n{random.choice(icons)}"

    @staticmethod
    def mutate_image(input_path: str, output_dir: str = "./temp_media") -> str:
        if not os.path.exists(input_path):
            return input_path
        os.makedirs(output_dir, exist_ok=True)
        img = Image.open(input_path).convert("RGB")
        width, height = img.size

        crop_px = random.randint(1, 2)
        if width > 10 and height > 10:
            img = img.crop((crop_px, crop_px, width - crop_px, height - crop_px))

        enhancer = ImageEnhance.Brightness(img)
        img_final = enhancer.enhance(random.uniform(0.98, 1.02))

        filename = f"mutated_{random.randint(10000, 99999)}_{os.path.basename(input_path)}"
        out_path = os.path.join(output_dir, filename)
        img_final.save(out_path, "JPEG", quality=random.randint(92, 96))
        return os.path.abspath(out_path)
