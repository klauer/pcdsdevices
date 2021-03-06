"""
PCDS plugins and Overrides for AreaDetector Plugins.
"""

import logging

import ophyd
import numpy as np
from ophyd import EpicsSignal, Component as C
from ophyd.device import GenerateDatumInterface
from ophyd.utils import set_and_wait
from ophyd.areadetector.base import ADBase

logger = logging.getLogger(__name__)


class PluginBase(ophyd.plugins.PluginBase, ADBase):
    """
    Overridden PluginBase to make it work when the root device is not a CamBase
    class.
    """
    enable = C(EpicsSignal, 'EnableCallbacks_RBV.RVAL', write_pv="EnableCallbacks", string=False)

    @property
    def source_plugin(self):
        # The PluginBase object that is the asyn source for this plugin.
        source_port = self.nd_array_port.get()
        if source_port == 'CAM' or not hasattr(
                self.root, 'get_plugin_by_asyn_port'):
            return None
        source_plugin = self.root.get_plugin_by_asyn_port(source_port)
        return source_plugin

    @property
    def _asyn_pipeline_configuration_names(self):
        # This broke any instantiated plugin b/c _asyn_pipeline is a list that
        # can have None.
        return [_.configuration_names.name for _ in self._asyn_pipeline if 
                hasattr(_, 'configuration_names')]

    @property
    def _asyn_pipeline(self):
        parent = None
        # Add a check to make sure root has this attr, otherwise return None
        if hasattr(self.root, 'get_plugin_by_asyn_port') and self.root != self:
            parent = self.root.get_plugin_by_asyn_port(self.nd_array_port.get())
            if hasattr(parent, '_asyn_pipeline'):
                return parent._asyn_pipeline + (self, )
        return (parent, self)

    def describe_configuration(self):
        # Use the overridden describe_configuration defined above
        ret = ADBase.describe_configuration(self)
        source_plugin = self.source_plugin
        if source_plugin is not None and source_plugin is not self:
            ret.update(source_plugin.describe_configuration())
        return ret

    def read_configuration(self):
        ret = ADBase.read_configuration(self)
        if self.source_plugin is not self:
            ret.update(self.source_plugin.read_configuration())
        return ret

    def stage(self):
        # Ensure the plugin is enabled. We do not disable it on unstage
        if self.enable not in self.stage_sigs:
            if not self.enable.connected:
                self.enable.get()
            set_and_wait(self.enable, 1, atol=0)
        ADBase.stage(self)

    @property
    def array_pixels(self):
        """
        The total number of pixels, calculated from array_size
        """
        array_size = list(self.array_size.get())
        dimensions = int(self.ndimensions.get())
        
        if dimensions == 0:
            return 0

        pixels = array_size[0]
        for dim in array_size[1:dimensions]:
            if dim:
                pixels *= dim

        return int(pixels)    

    
class ImagePlugin(ophyd.plugins.ImagePlugin, PluginBase):
    @property
    def image(self):
        """
        Overriden image method to add in some corrections
        """
        array_size = [int(val) for val in self.array_size.get()]
        if array_size == [0, 0, 0]:
            raise RuntimeError('Invalid image; ensure array_callbacks are on')

        if array_size[-1] == 0:
            array_size = array_size[:-1]

        pixel_count = self.array_pixels
        image = self.array_data.get(count=pixel_count)
        return np.array(image).reshape(array_size)    

    
class StatsPlugin(ophyd.plugins.StatsPlugin, PluginBase):
    pass


class ColorConvPlugin(ophyd.plugins.ColorConvPlugin, PluginBase):
    pass


class ProcessPlugin(ophyd.plugins.ProcessPlugin, PluginBase):
    pass


class Overlay(ophyd.plugins.Overlay, ADBase):
    pass


class OverlayPlugin(ophyd.plugins.OverlayPlugin, PluginBase):
    pass


class ROIPlugin(ophyd.plugins.ROIPlugin, PluginBase):
    pass


class TransformPlugin(ophyd.plugins.TransformPlugin, PluginBase):
    pass


class FilePlugin(ophyd.plugins.FilePlugin, PluginBase, GenerateDatumInterface):
    pass


class NetCDFPlugin(ophyd.plugins.NetCDFPlugin, FilePlugin):
    pass


class TIFFPlugin(ophyd.plugins.TIFFPlugin, FilePlugin):
    pass


class JPEGPlugin(ophyd.plugins.JPEGPlugin, FilePlugin):
    pass


class NexusPlugin(ophyd.plugins.NexusPlugin, FilePlugin):
    pass


class HDF5Plugin(ophyd.plugins.HDF5Plugin, FilePlugin):
    pass


class MagickPlugin(ophyd.plugins.MagickPlugin, FilePlugin):
    pass


