import Image
import matplotlib.pyplot as plt
import os
import numpy as np

from workspace_generation_utils import WorkspaceParams


class ImageCacheItem:
    def __init__(self, workspace_id, full_filename, params, np_array):
        self.workspace_id = workspace_id
        self.full_filename = full_filename
        self.params = params
        self.np_array = np_array


class ImageCache:
    def __init__(self, params_directory):
        self.items = {}

        source_dir = os.path.expanduser(params_directory)
        for dirpath, dirnames, filenames in os.walk(source_dir):
            for filename in filenames:
                if not filename.endswith('.pkl'):
                    continue
                full_file_path = os.path.join(source_dir, filename)
                params = WorkspaceParams.load_from_file(full_file_path)
                # np_array = self._get_image_as_numpy(params)

                # self.items[filename] = ImageCacheItem(filename, full_file_path, params, np_array)
                self.items[filename] = ImageCacheItem(filename, full_file_path, params, None)

    @staticmethod
    def _figure_to_nparray(fig):
        fig.canvas.draw()
        w, h = fig.canvas.get_width_height()
        buf = np.fromstring(fig.canvas.tostring_argb(), dtype=np.uint8)
        buf.shape = (w, h, 4)
        buf = np.roll(buf, 3, axis=2)
        return buf

    @staticmethod
    def _figure_to_image(fig):
        buf = ImageCache._figure_to_nparray(fig)
        w, h, d = buf.shape
        return Image.frombytes("RGBA", (w, h), buf.tobytes())

    @staticmethod
    def _remove_transparency(im, bg_colour=(255, 255, 255)):
        if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):

            # Need to convert to RGBA if LA format due to a bug in PIL
            alpha = im.convert('RGBA').split()[-1]

            # Create a new background image of our matt color.
            # Must be RGBA because paste requires both images have the same format

            bg = Image.new("RGBA", im.size, bg_colour + (255,))
            bg.paste(im, mask=alpha)
            return bg

        else:
            return im

    @staticmethod
    def _get_image_as_numpy(params):
        f = params.print_image()
        im = ImageCache._figure_to_image(f)
        im = ImageCache._remove_transparency(im).convert('L')
        im = im.crop((73, 108, 517, 330))
        width = im.width / 4
        height = im.height / 4
        im.thumbnail((width, height), Image.ANTIALIAS)
        res = np.asarray(im)
        plt.clf()
        return res
