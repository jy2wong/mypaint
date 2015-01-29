# This file is part of MyPaint.
# Copyright (C) 2007-2013 by Martin Renold <martinxyz@gmx.ch>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

## Imports

import os
import sys
import zipfile
import tempfile
import time
import traceback
from os.path import join
from cStringIO import StringIO
import xml.etree.ElementTree as ET
from warnings import warn
import logging
logger = logging.getLogger(__name__)

from gi.repository import GdkPixbuf
from gi.repository import GObject

import numpy
from gettext import gettext as _

import helpers
import fileutils
import tiledsurface
import pixbufsurface
import mypaintlib
import command
import stroke
import layer
import brush
from observable import event
import lib.pixbuf


## Module constants

DEFAULT_RESOLUTION = 72

N = tiledsurface.N


## Class defs

class SaveLoadError(Exception):
    """Expected errors on loading or saving

    Covers stuff like missing permissions or non-existing files.

    """
    pass


class Document (object):
    """In-memory representation of everything to be worked on & saved

    This is the "model" in the Model-View-Controller design for the
    drawing canvas. The View mostly resides in `gui.tileddrawwidget`,
    and the Controller is mostly in `gui.document` and `gui.mode`.

    The model contains everything that the user would want to save. It
    is possible to use the model without any GUI attached (see
    ``../tests/``).
    """
    # Please note the following difficulty with the undo stack:
    #
    #   Most of the time there is an unfinished (but already rendered)
    #   stroke pending, which has to be turned into a command.Action
    #   or discarded as empty before any other action is possible.

    ## Class constants

    TEMPDIR_STUB_NAME = "mypaint"

    #: Debugging toggle. If True, New and Load and Remove Layer will create a
    #: new blank painting layer if they empty out the document.
    CREATE_PAINTING_LAYER_IF_EMPTY = True

    ## Initialization and cleanup

    def __init__(self, brushinfo=None, painting_only=False):
        """Initialize

        :param brushinfo: the lib.brush.BrushInfo instance to use
        :param painting_only: only use painting layers

        If painting_only is true, then no tempdir will be created by the
        document when it is initialized or cleared.
        """
        object.__init__(self)
        if not brushinfo:
            brushinfo = brush.BrushInfo()
            brushinfo.load_defaults()
        self._layers = layer.RootLayerStack(self)
        self._layers.layer_content_changed += self._canvas_modified_cb
        self.brush = brush.Brush(brushinfo)
        self.brush.brushinfo.observers.append(self.brushsettings_changed_cb)
        self.stroke = None
        self.command_stack = command.CommandStack()
        self._painting_only = painting_only
        self._tempdir = None

        # Optional page area and resolution information
        self._frame = [0, 0, 0, 0]
        self._frame_enabled = False
        self._xres = None
        self._yres = None

        # Backgrounds for rendering
        blank_arr = numpy.zeros((N, N, 4), dtype='uint16')
        self._blank_bg_surface = tiledsurface.Background(blank_arr)

        self.clear()

    def __repr__(self):
        bbox = self.get_bbox()
        nlayers = len(list(self.layer_stack.deepenumerate()))
        return ("<Document nlayers=%d bbox=%r paintonly=%r>" %
                (nlayers, bbox, self._painting_only))

    ## Layer stack access

    @property
    def layer_stack(self):
        """The root of the layer stack tree

        See also `lib.layer.RootLayerStack`.
        """
        # TODO: rename or alias this to just "layers" one day.
        return self._layers

    ## Working-doc tempdir

    @property
    def tempdir(self):
        """The working document's tempdir (read-only)"""
        return self._tempdir

    def _create_tempdir(self):
        """Internal: creates the working-document tempdir"""
        if self._painting_only:
            return
        assert self._tempdir is None
        tempdir = tempfile.mkdtemp(self.TEMPDIR_STUB_NAME)
        if not isinstance(tempdir, unicode):
            tempdir = tempdir.decode(sys.getfilesystemencoding())
        logger.debug("Created working-doc tempdir %r", tempdir)
        self._tempdir = tempdir

    def _cleanup_tempdir(self):
        """Internal: recursively delete the working-document tempdir"""
        if self._painting_only:
            return
        assert self._tempdir is not None
        tempdir = self._tempdir
        self._tempdir = None
        for root, dirs, files in os.walk(tempdir, topdown=False):
            for name in files:
                tempfile = os.path.join(root, name)
                try:
                    os.remove(tempfile)
                except OSError, err:
                    logger.warning("Cannot remove %r: %r", tempfile, err)
            for name in dirs:
                subtemp = os.path.join(root, name)
                try:
                    os.rmdir(subtemp)
                except OSError, err:
                    logger.warning("Cannot rmdir %r: %r", subtemp, err)
        try:
            os.rmdir(tempdir)
        except OSError, err:
            logger.warning("Cannot rmdir %r: %r", subtemp, err)
        if os.path.exists(tempdir):
            logger.error("Failed to remove working-doc tempdir %r", tempdir)
        else:
            logger.debug("Successfully removed working-doc tempdir %r", tempdir)

    def cleanup(self):
        """Cleans up any persistent state belonging to the document.

        Currently this just removes the working-document tempdir. This method
        is called by the main app's exit routine after confirmation.
        """
        self._cleanup_tempdir()

    ## Document frame

    def get_resolution(self):
        """Returns the document model's nominal resolution

        The OpenRaster format saves resolution information in both vertical and
        horizontal resolutions, but MyPaint does not support this at present.
        This method returns the a unidirectional document resolution in pixels
        per inch; this is the user-chosen factor that UI controls should use
        when converting real-world measurements in frames, fonts, and other
        objects to document pixels.

        Note that the document resolution has no direct relation to screen
        pixels or printed dots.
        """
        if self._xres and self._yres:
            return max(1, max(self._xres, self._yres))
        else:
            return DEFAULT_RESOLUTION

    def set_resolution(self, res):
        """Sets the document model's nominal resolution

        The OpenRaster format saves resolution information in both vertical and
        horizontal resolutions, but MyPaint does not support this at present.
        This method sets the document resolution in pixels per inch in both
        directions.

        Note that the document resolution has no direct relation to screen
        pixels or printed dots.
        """
        if res is not None:
            res = int(res)
            res = max(1, res)
        # Maybe. Using 72 as a fake null would be pretty weird.
        #if res == DEFAULT_RESOLUTION:
        #    res = None
        self._xres = res
        self._yres = res

    def get_frame(self):
        return self._frame

    def set_frame(self, frame, user_initiated=False):
        x, y, w, h = frame
        self.update_frame(x=x, y=y, width=w, height=h,
                          user_initiated=user_initiated)

    frame = property(get_frame, set_frame)

    def update_frame(self, x=None, y=None, width=None, height=None,
                     user_initiated=False):
        """Update parts of the frame"""
        frame = [x, y, width, height]
        if user_initiated:
            if isinstance(self.get_last_command(), command.UpdateFrame):
                self.update_last_command(frame=frame)
            else:
                self.do(command.UpdateFrame(self, frame))
        else:
            new_frame = list(self._frame[:])
            for i, var in enumerate([x, y, width, height]):
                if var is not None:
                    new_frame[i] = int(var)
            if new_frame != self._frame:
                old_frame = tuple(self._frame)
                self._frame[:] = new_frame
                new_frame = tuple(new_frame)
                self.frame_updated(old_frame, new_frame)

    @event
    def frame_updated(self, old_frame, new_frame):
        """Event: the frame's dimensions were updated

        :param tuple frame: the new frame extents (x, y, w, h)
        """

    def get_frame_enabled(self):
        return self._frame_enabled

    def set_frame_enabled(self, enabled, user_initiated=False):
        enabled = bool(enabled)
        if self._frame_enabled == enabled:
            return
        if user_initiated:
            self.do(command.SetFrameEnabled(self, enabled))
        else:
            self._frame_enabled = enabled
            self.frame_enabled_changed(enabled)

    frame_enabled = property(get_frame_enabled)

    @event
    def frame_enabled_changed(self, enabled):
        """Event: the frame_enabled field changed value"""

    def set_frame_to_current_layer(self, user_initiated=False):
        current = self.layer_stack.current
        x, y, w, h = current.get_bbox()
        self.update_frame(x, y, w, h, user_initiated=user_initiated)

    def set_frame_to_document(self, user_initiated=False):
        x, y, w, h = self.get_bbox()
        self.update_frame(x, y, w, h, user_initiated=user_initiated)

    def trim_current_layer(self):
        """Trim the current layer to the extent of the document frame

        This has no effect if the frame is not currently enabled.

        """
        if not self._frame_enabled:
            return
        self.do(command.TrimLayer(self))

    ## Misc actions

    def clear(self):
        """Clears everything, and resets the command stack

        This results in a document consisting of
        one newly created blank drawing layer,
        an empty undo history,
        and a new empty working-document temp directory.
        Clearing the document also generates a full redraw,
        and resets the frame and the stored resolution.
        """
        self.flush_updates()
        self._layers.set_symmetry_state(False, None)
        prev_area = self.get_full_redraw_bbox()
        if self._tempdir is not None:
            self._cleanup_tempdir()
        self._create_tempdir()
        self.command_stack.clear()
        self._layers.clear()
        if self.CREATE_PAINTING_LAYER_IF_EMPTY:
            self.add_layer((-1,))
            self._layers.current_path = (0,)
            self.command_stack.clear()
        else:
            self._layers.current_path = None
        self.unsaved_painting_time = 0.0
        self.set_frame([0, 0, 0, 0])
        self.set_frame_enabled(False)
        self._xres = None
        self._yres = None
        self.canvas_area_modified(*prev_area)

    def brushsettings_changed_cb(self, settings, lightweight_settings=set([
            'radius_logarithmic', 'color_h', 'color_s', 'color_v',
            'opaque', 'hardness', 'slow_tracking', 'slow_tracking_per_dab'
            ])):
        # The lightweight brush settings are expected to change often in
        # mid-stroke e.g. by heavy keyboard usage. If only those change, we
        # don't create a new undo step. (And thus also no separate pickable
        # stroke in the strokemap.)
        if settings - lightweight_settings:
            self.flush_updates()

    def select_layer(self, index=None, path=None, layer=None):
        """Selects a layer undoably"""
        layers = self.layer_stack
        sel_path = layers.canonpath(index=index, path=path, layer=layer,
                                    usecurrent=False, usefirst=True)
        self.do(command.SelectLayer(self, path=sel_path))

    ## Layer stack (z-order and grouping)

    def restack_layer(self, src_path, targ_path):
        """Moves a layer within the layer stack by path, undoably

        :param tuple src_path: path of the layer to be moved
        :param tuple targ_path: target insert path

        The source path must identify an existing layer. The target
        path must be a valid insertion path at the time this method is
        called.
        """
        logger.debug("Restack layer at %r to %r", src_path, targ_path)
        cmd = command.RestackLayer(self, src_path, targ_path)
        self.do(cmd)

    def bubble_current_layer_up(self):
        """Moves the current layer up in the stack (undoable)"""
        cmd = command.BubbleLayerUp(self)
        self.do(cmd)

    def bubble_current_layer_down(self):
        """Moves the current layer down in the stack (undoable)"""
        cmd = command.BubbleLayerDown(self)
        self.do(cmd)

    ## Misc layer command frontends

    def duplicate_current_layer(self):
        """Makes an exact copy of the current layer (undoable)"""
        self.do(command.DuplicateLayer(self))

    def clear_current_layer(self):
        """Clears the current layer (undoable)"""
        rootstack = self.layer_stack
        can_clear = (rootstack.current is not rootstack
                     and not rootstack.current.is_empty())
        if not can_clear:
            return
        self.do(command.ClearLayer(self))

    ## Drawing/painting strokes

    def redo_last_stroke_with_different_brush(self, brushinfo):
        cmd = self.get_last_command()
        if not isinstance(cmd, command.Brushwork):
            return
        cmd.update(brushinfo=brushinfo)

    ## Other painting/drawing

    def flood_fill(self, x, y, color, tolerance=0.1,
                   sample_merged=False, make_new_layer=False):
        """Flood-fills a point on the current layer with a color

        :param x: Starting point X coordinate
        :param y: Starting point Y coordinate
        :param color: The RGB color to fill connected pixels with
        :type color: tuple
        :param tolerance: How much filled pixels are permitted to vary
        :type tolerance: float [0.0, 1.0]
        :param sample_merged: Use all visible layers when sampling
        :type sample_merged: bool
        :param make_new_layer: Write output to a new layer on top
        :type make_new_layer: bool

        Filling an infinite canvas requires limits. If the frame is
        enabled, this limits the maximum size of the fill, and filling
        outside the frame is not possible.

        Otherwise, if the entire document is empty, the limits are
        dynamic.  Initially only a single tile will be filled. This can
        then form one corner for the next fill's limiting rectangle.
        This is a little quirky, but allows big areas to be filled
        rapidly as needed on blank layers.
        """
        bbox = helpers.Rect(*tuple(self.get_effective_bbox()))
        if not self.layer_stack.current.get_fillable():
            make_new_layer = True
        if bbox.empty():
            bbox = helpers.Rect()
            bbox.x = N*int(x//N)
            bbox.y = N*int(y//N)
            bbox.w = N
            bbox.h = N
        elif not self.frame_enabled:
            bbox.expandToIncludePoint(x, y)
        cmd = command.FloodFill(self, x, y, color, bbox, tolerance,
                                sample_merged, make_new_layer)
        self.do(cmd)

    ## Graphical refresh

    def _canvas_modified_cb(self, root, layer, x, y, w, h):
        """Internal callback: forwards redraw nofifications"""
        self.canvas_area_modified(x, y, w, h)

    @event
    def canvas_area_modified(self, x, y, w, h):
        """Event: canvas was updated, either within a rectangle or fully

        :param x: top-left x coordinate for the redraw bounding box
        :param y: top-left y coordinate for the redraw bounding box
        :param w: width of the redraw bounding box, or 0 for full redraw
        :param h: height of the redraw bounding box, or 0 for full redraw

        This event method is invoked to notify observers about needed redraws
        originating from within the model, e.g. painting, fills, or layer
        moves. It is also used to notify about the entire canvas needing to be
        redrawn. In the latter case, the `w` or `h` args forwarded to
        registered observers is zero.

        See also: `invalidate_all()`.
        """
        pass

    def invalidate_all(self):
        """Marks everything as invalid"""
        self.canvas_area_modified(0, 0, 0, 0)

    ## Undo/redo command stack

    @event
    def flush_updates(self):
        """Reqests flushing of all pending document updates

        This `lib.observable.event` is called whan pending updates
        should be flushed into the working document completely.
        Attached observers are expected to react by writing pending
        changes to the layers stack, and pushing an appropriate command
        onto the command stack using `do()`.
        """

    def undo(self):
        self.flush_updates()
        while 1:
            cmd = self.command_stack.undo()
            if not cmd or not cmd.automatic_undo:
                return cmd

    def redo(self):
        self.flush_updates()
        while 1:
            cmd = self.command_stack.redo()
            if not cmd or not cmd.automatic_undo:
                return cmd

    def do(self, cmd):
        self.flush_updates()
        self.command_stack.do(cmd)

    def update_last_command(self, **kwargs):
        self.flush_updates()
        return self.command_stack.update_last_command(**kwargs)

    def get_last_command(self):
        self.flush_updates()
        return self.command_stack.get_last_command()

    ## Utility methods

    def get_bbox(self):
        """Returns the data bounding box of the document

        This is currently the union of all the data bounding boxes of all of
        the layers. It disregards the user-chosen frame.

        """
        res = helpers.Rect()
        for layer in self.layer_stack.deepiter():
            # OPTIMIZE: only visible layers...
            # careful: currently saving assumes that all layers are included
            bbox = layer.get_bbox()
            res.expandToIncludeRect(bbox)
        return res

    def get_full_redraw_bbox(self):
        """Returns the full-redraw bounding box of the document

        This is the same concept as `layer.BaseLayer.get_full_redraw_bbox()`,
        and is built up from the full-redraw bounding boxes of all layers.
        """
        res = helpers.Rect()
        for layer in self.layer_stack.deepiter():
            bbox = layer.get_full_redraw_bbox()
            if bbox.w == 0 and bbox.h == 0:  # infinite
                res = bbox
            else:
                res.expandToIncludeRect(bbox)
        return res

    def get_effective_bbox(self):
        """Return the effective bounding box of the document.

        If the frame is enabled, this is the bounding box of the frame,
        else the (dynamic) bounding box of the document.

        """
        return self.get_frame() if self.frame_enabled else self.get_bbox()

    ## Rendering tiles

    def blit_tile_into(self, dst, dst_has_alpha, tx, ty, mipmap_level=0,
                       layers=None, background=None):
        """Blit composited tiles into a destination surface"""
        self.layer_stack.blit_tile_into(
            dst, dst_has_alpha, tx, ty,
            mipmap_level, layers=layers
        )

    ## More layer stack commands

    def add_layer(self, path, layer_class=layer.PaintingLayer, **kwds):
        """Undoably adds a new layer at a specified path

        :param path: Path for the new layer
        :param callable layer_class: constructor for the new layer
        :param **kwds: Constructor args

        By default, a normal painting layer is added.

        See: `lib.command.AddLayer`
        """
        self.do(command.AddLayer(
            self, path,
            name=None,
            layer_class=layer_class,
            **kwds
            ))

    def remove_current_layer(self):
        """Delete the current layer"""
        if not self.layer_stack.current_path:
            return
        self.do(command.RemoveLayer(self))

    def rename_current_layer(self, name):
        """Rename the current layer"""
        if not self.layer_stack.current_path:
            return
        self.do(command.RenameLayer(self, name))

    def normalize_layer_mode(self):
        """Normalize current layer's mode and opacity"""
        layers = self.layer_stack
        self.do(command.NormalizeLayerMode(self, layers.current))

    def merge_current_layer_down(self):
        """Merge the current layer into the one below"""
        rootstack = self.layer_stack
        cur_path = rootstack.current_path
        if cur_path is None:
            return False
        dst_path = rootstack.get_merge_down_target(cur_path)
        if dst_path is None:
            logger.info("Merge Down is not possible here")
            return False
        self.do(command.MergeLayerDown(self))
        return True

    def merge_visible_layers(self):
        self.do(command.MergeVisibleLayers(self))

    ## Layer import/export

    def load_layer_from_pixbuf(self, pixbuf, x=0, y=0):
        arr = helpers.gdkpixbuf2numpy(pixbuf)
        s = tiledsurface.Surface()
        bbox = s.load_from_numpy(arr, x, y)
        self.do(command.LoadLayer(self, s))
        return bbox

    def load_layer_from_png(self, filename, x=0, y=0, feedback_cb=None):
        s = tiledsurface.Surface()
        bbox = s.load_from_png(filename, x, y, feedback_cb)
        self.do(command.LoadLayer(self, s))
        return bbox

    def update_layer_from_external_edit_tempfile(self, layer, file_path):
        """Update a layer after external edits to its tempfile"""
        assert hasattr(layer, "load_from_external_edit_tempfile")
        cmd = command.ExternalLayerEdit(self, layer, file_path)
        self.do(cmd)

    ## Even more layer command frontends

    def set_layer_visibility(self, visible, layer):
        """Sets the visibility of a layer."""
        if layer is self.layer_stack:
            return
        cmd_class = command.SetLayerVisibility
        cmd = self.get_last_command()
        if isinstance(cmd, cmd_class) and cmd.layer is layer:
            self.update_last_command(visible=visible)
        else:
            cmd = cmd_class(self, visible, layer)
            self.do(cmd)

    def set_layer_locked(self, locked, layer):
        """Sets the input-locked status of a layer."""
        if layer is self.layer_stack:
            return
        cmd_class = command.SetLayerLocked
        cmd = self.get_last_command()
        if isinstance(cmd, cmd_class) and cmd.layer is layer:
            self.update_last_command(locked=locked)
        else:
            cmd = cmd_class(self, locked, layer)
            self.do(cmd)

    def set_current_layer_opacity(self, opacity):
        """Sets the opacity of the current layer

        :param float opacity: New layer opacity
        """
        current = self.layer_stack.current
        if current is self.layer_stack:
            return
        if current.mode == layer.PASS_THROUGH_MODE:
            return
        cmd_class = command.SetLayerOpacity
        cmd = self.get_last_command()
        if isinstance(cmd, cmd_class) and cmd.layer is current:
            logger.debug("Updating current layer opacity: %r", opacity)
            self.update_last_command(opacity=opacity)
        else:
            logger.debug("Setting current layer opacity: %r", opacity)
            cmd = cmd_class(self, opacity, layer=current)
            self.do(cmd)

    def set_current_layer_mode(self, mode):
        """Sets the mode for the current layer

        :param int mode: New layer mode to use
        """
        current = self.layer_stack.current
        if current is self.layer_stack:
            return
        logger.debug("Setting current layer mode: %r", mode)
        cmd = command.SetLayerMode(self, mode, layer=current)
        self.do(cmd)

    ## Saving and loading

    def load_from_pixbuf(self, pixbuf):
        """Load a document from a pixbuf."""
        self.clear()
        bbox = self.load_layer_from_pixbuf(pixbuf)
        self.set_frame(bbox, user_initiated=False)

    def save(self, filename, **kwargs):
        """Save the document to a file.

        :param str filename: The filename to save to.
        :param dict kwargs: Passed on to the chosen save method.
        :raise SaveLoadError: The error string will be set to something
          descriptive and presentable to the user.
        :returns: A thumbnail pixbuf, or None if not supported
        :rtype: GdkPixbuf

        The filename's extension is used to determine the save format, and a
        ``save_*()`` method is chosen to perform the save.
        """
        self.flush_updates()
        junk, ext = os.path.splitext(filename)
        ext = ext.lower().replace('.', '')
        save = getattr(self, 'save_' + ext, self._unsupported)
        result = None
        try:
            result = save(filename, **kwargs)
        except GObject.GError, e:
            traceback.print_exc()
            if e.code == 5:
                #add a hint due to a very consfusing error message when there is no space left on device
                raise SaveLoadError(_('Unable to save: %s\nDo you have enough space left on the device?') % e.message)
            else:
                raise SaveLoadError(_('Unable to save: %s') % e.message)
        except IOError, e:
            traceback.print_exc()
            raise SaveLoadError(_('Unable to save: %s') % e.strerror)
        self.unsaved_painting_time = 0.0
        return result

    def load(self, filename, **kwargs):
        """Load the document from a file.

        :param str filename:
            The filename to load from. The extension is used to determine
            format, and a ``load_*()`` method is chosen to perform the load.
        :param dict kwargs:
            Passed on to the chosen loader method.
        :raise SaveLoadError:
            The error string will be set to something descriptive and
            presentable to the user.

        """
        if not os.path.isfile(filename):
            raise SaveLoadError(_('File does not exist: %s') % repr(filename))
        if not os.access(filename, os.R_OK):
            raise SaveLoadError(_('You do not have the necessary permissions to open file: %s') % repr(filename))
        junk, ext = os.path.splitext(filename)
        ext = ext.lower().replace('.', '')
        load = getattr(self, 'load_' + ext, self._unsupported)
        try:
            load(filename, **kwargs)
        except GObject.GError, e:
            traceback.print_exc()
            raise SaveLoadError(_('Error while loading: GError %s') % e)
        except IOError, e:
            traceback.print_exc()
            raise SaveLoadError(_('Error while loading: IOError %s') % e)
        self.command_stack.clear()
        self.unsaved_painting_time = 0.0

    def _unsupported(self, filename, *args, **kwargs):
        raise SaveLoadError(_('Unknown file format extension: %s') % repr(filename))

    def render_thumbnail(self, **kwargs):
        """Renders a thumbnail for the effective (frame) bbox"""
        t0 = time.time()
        bbox = self.get_effective_bbox()
        pixbuf = self.layer_stack.render_thumbnail(bbox, **kwargs)
        logger.info('Rendered thumbnail in %d seconds.',
                    time.time() - t0)
        return pixbuf

    def save_png(self, filename, alpha=True, multifile=False, **kwargs):
        """Save to one or more PNG files"""
        if multifile:
            self._save_multi_file_png(filename, **kwargs)
        else:
            self._save_single_file_png(filename, alpha, **kwargs)

    def _save_single_file_png(self, filename, alpha, **kwargs):
        doc_bbox = self.get_effective_bbox()
        self.layer_stack.save_as_png(
            filename,
            *doc_bbox,
            alpha=alpha,
            background=not alpha,
            **kwargs
        )

    def _save_multi_file_png(self, filename, **kwargs):
        """Save to multiple suffixed PNG files"""
        prefix, ext = os.path.splitext(filename)
        # if we have a number already, strip it
        l = prefix.rsplit('.', 1)
        if l[-1].isdigit():
            prefix = l[0]
        doc_bbox = self.get_effective_bbox()
        for i, l in enumerate(self.layer_stack.deepiter()):
            filename = '%s.%03d%s' % (prefix, i+1, ext)
            l.save_as_png(filename, *doc_bbox, alpha=True, **kwargs)

    def load_png(self, filename, feedback_cb=None):
        """Load (speedily) from a PNG file"""
        self.clear()
        bbox = self.load_layer_from_png(filename, 0, 0, feedback_cb)
        self.set_frame(bbox, user_initiated=False)

    def load_from_pixbuf_file(self, filename, feedback_cb=None):
        """Load from a file which GdkPixbuf can open"""
        pixbuf = lib.pixbuf.load_from_file(filename, feedback_cb)
        self.load_from_pixbuf(pixbuf)

    load_jpg = load_from_pixbuf_file
    load_jpeg = load_from_pixbuf_file

    @fileutils.via_tempfile
    def save_jpg(self, filename, quality=90, **kwargs):
        x, y, w, h = self.get_effective_bbox()
        if w == 0 or h == 0:
            x, y, w, h = 0, 0, N, N  # allow to save empty documents
        pixbuf = self.layer_stack.render_as_pixbuf(x, y, w, h, **kwargs)
        lib.pixbuf.save(pixbuf, filename, 'jpeg', quality=str(quality))

    save_jpeg = save_jpg

    @fileutils.via_tempfile
    def save_ora(self, filename, options=None, **kwargs):
        """Saves OpenRaster data to a file"""
        logger.info('save_ora: %r (%r, %r)', filename, options, kwargs)
        t0 = time.time()
        tempdir = tempfile.mkdtemp('mypaint')
        if not isinstance(tempdir, unicode):
            tempdir = tempdir.decode(sys.getfilesystemencoding())

        orazip = zipfile.ZipFile(filename, 'w',
                                 compression=zipfile.ZIP_STORED)

        # work around a permission bug in the zipfile library:
        # http://bugs.python.org/issue3394
        def write_file_str(filename, data):
            zi = zipfile.ZipInfo(filename)
            zi.external_attr = 0100644 << 16
            orazip.writestr(zi, data)

        write_file_str('mimetype', 'image/openraster')  # must be the first file
        image = ET.Element('image')
        effective_bbox = self.get_effective_bbox()
        x0, y0, w0, h0 = effective_bbox
        image.attrib['w'] = str(w0)
        image.attrib['h'] = str(h0)

        # Update the initially-selected flag on all layers
        layers = self.layer_stack
        for s_path, s_layer in layers.walk():
            selected = (s_path == layers.current_path)
            s_layer.initially_selected = selected

        # Save the layer stack
        canvas_bbox = tuple(self.get_bbox())
        frame_bbox = tuple(effective_bbox)
        root_stack_path = ()
        root_stack_elem = self.layer_stack.save_to_openraster(
            orazip, tempdir, root_stack_path,
            canvas_bbox, frame_bbox, **kwargs
        )
        image.append(root_stack_elem)

        # Resolution info
        if self._xres and self._yres:
            image.attrib["xres"] = str(self._xres)
            image.attrib["yres"] = str(self._yres)

        # OpenRaster version declaration
        image.attrib["version"] = "0.0.4-pre.1"

        # Thumbnail preview (256x256)
        thumbnail = layers.render_thumbnail(frame_bbox)
        tmpfile = join(tempdir, 'tmp.png')
        lib.pixbuf.save(thumbnail, tmpfile, 'png')
        orazip.write(tmpfile, 'Thumbnails/thumbnail.png')
        os.remove(tmpfile)

        # Save fully rendered image too
        tmpfile = os.path.join(tempdir, "mergedimage.png")
        self.layer_stack.save_as_png(
            tmpfile, *frame_bbox,
            alpha=False, background=True,
            **kwargs
        )
        orazip.write(tmpfile, 'mergedimage.png')
        os.remove(tmpfile)

        # Prettification
        helpers.indent_etree(image)
        xml = ET.tostring(image, encoding='UTF-8')

        # Finalize
        write_file_str('stack.xml', xml)
        orazip.close()
        os.rmdir(tempdir)

        logger.info('%.3fs save_ora total', time.time() - t0)
        return thumbnail

    def load_ora(self, filename, feedback_cb=None):
        """Loads from an OpenRaster file"""
        logger.info('load_ora: %r', filename)
        t0 = time.time()
        tempdir = self._tempdir
        orazip = zipfile.ZipFile(filename)
        logger.debug('mimetype: %r', orazip.read('mimetype').strip())
        xml = orazip.read('stack.xml')
        image_elem = ET.fromstring(xml)
        root_stack_elem = image_elem.find('stack')
        image_width = max(0, int(image_elem.attrib.get('w', 0)))
        image_height = max(0, int(image_elem.attrib.get('h', 0)))
        # Resolution: false value, 0 specifically, means unspecified
        image_xres = max(0, int(image_elem.attrib.get('xres', 0)))
        image_yres = max(0, int(image_elem.attrib.get('yres', 0)))

        # Delegate loading of image data to the layers tree itself
        self.layer_stack.clear()
        self.layer_stack.load_from_openraster(orazip, root_stack_elem,
                                              tempdir, feedback_cb, x=0, y=0)
        assert len(self.layer_stack) > 0

        # Resolution information if specified
        # Before frame to benefit from its observer call
        if image_xres and image_yres:
            self._xres = image_xres
            self._yres = image_yres
        else:
            self._xres = None
            self._yres = None

        # Set the frame size to that saved in the image.
        self.update_frame(x=0, y=0, width=image_width, height=image_height,
                          user_initiated=False)

        # Enable frame if the saved image size is something other than the
        # calculated bounding box. Goal: if the user saves an "infinite
        # canvas", it loads as an infinite canvas.
        bbox_c = helpers.Rect(x=0, y=0, w=image_width, h=image_height)
        bbox = self.get_bbox()
        frame_enab = not (bbox_c == bbox or bbox.empty() or bbox_c.empty())
        self.set_frame_enabled(frame_enab, user_initiated=False)

        orazip.close()

        logger.info('%.3fs load_ora total', time.time() - t0)
