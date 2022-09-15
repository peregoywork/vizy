#
# This file is part of Vizy 
#
# All Vizy source code is provided under the terms of the
# GNU General Public License v2 (http://www.gnu.org/licenses/gpl-2.0.html).
# Those wishing to use Vizy source code, software and/or
# technologies under different licensing terms should contact us at
# support@charmedlabs.com. 
#

import os
import cv2
import time
import json
import datetime
import numpy as np
from threading import Thread, RLock
import kritter
from kritter import get_color
from kritter.tflite import TFliteClassifier, TFliteDetector
from dash_devices.dependencies import Input, Output
import dash_html_components as html
from vizy import Vizy
import vizy.vizypowerboard as vpb
from handle_event import handle_event
from kritter.ktextvisor import KtextVisor, KtextVisorTable

MIN_THRESHOLD = 0.1
MAX_THRESHOLD = 0.9
THRESHOLD_HYSTERESIS = 0.2
CAMERA_MODE = "1920x1080x10bpp"
STREAM_WIDTH = 800

CONFIG_FILE = "birdfeeder.json"
CONSTS_FILE = "birdfeeder_consts.py"

DEFAULT_CONFIG = {
    "brightness": 50,
    "detection_threshold": 50,
    "enabled_classes": None,
    "trigger_classes": [],
    "gphoto_upload": False
}

BASEDIR = os.path.dirname(os.path.realpath(__file__))
MEDIA_DIR = os.path.join(BASEDIR, "media")
class BirdInference:

    def __init__(self):
        self.detector = TFliteDetector(os.path.join(BASEDIR, "bird_detector.tflite"))
        self.classifier = TFliteClassifier(os.path.join(BASEDIR, "north_american_bird_classifier.tflite"))

    def detect(self, image, threshold=0.75):
        dets = self.detector.detect(image, threshold)
        res = []
        for d in dets:
            if d['class']=="Bird":
                box = d['box']
                bird = image[box[1]:box[3], box[0]:box[2]]
                bird_type = self.classifier.classify(bird)
                obj = {"class": bird_type[0]['class'], "score": bird_type[0]['score'], "box": box}
            else:
                obj = d
            res.append(obj)
        return res

    def classes(self):
        return self.classifier.classes()

WAITING = 0
RECORDING = 1
SAVING = 2

class Birdfeeder:
    def __init__(self):

        # Create Kritter server.
        self.kapp = Vizy()
        self.kapp.media_path.insert(0, MEDIA_DIR)

        # Initialize variables.
        config_filename = os.path.join(self.kapp.etcdir, CONFIG_FILE)      
        self.config = kritter.ConfigFile(config_filename, DEFAULT_CONFIG)               
        consts_filename = os.path.join(BASEDIR, CONSTS_FILE) 
        self.config_consts = kritter.import_config(consts_filename, self.kapp.etcdir, ["IMAGES_KEEP", "IMAGES_DISPLAY", "PICKER_TIMEOUT", "GPHOTO_ALBUM", "MARQUEE_IMAGE_WIDTH", "DEFEND_BIT"]) 
        self.lock = RLock()
        self.record = None
        self.record_state = WAITING

        # Initialize power board defense bit.
        self.kapp.power_board.vcc12(True)
        self.kapp.power_board.io_set_mode(self.config_consts.DEFEND_BIT, vpb.IO_MODE_HIGH_CURRENT)
        self.kapp.power_board.io_set_bit(self.config_consts.DEFEND_BIT) # set defend bit to high (turn off)

        # Create and start camera.
        self.camera = kritter.Camera(hflip=True, vflip=True, mem_reserve=50)
        self.stream = self.camera.stream()
        self.camera.mode = CAMERA_MODE
        self.camera.brightness = self.config['brightness']
        self.camera.framerate = 20
        self.camera.autoshutter = True
        self.camera.awb = True

        # Invoke KtextVisor client, which relies on the server running.
        # In case it isn't running, we just roll with it.  
        try:
            self.tv = KtextVisor()
            print("*** Texting interface found!")
        except:
            self.tv = None
            print("*** Texting interface not found.")

        self.gcloud = kritter.Gcloud(self.kapp.etcdir)
        self.gphoto_interface = self.gcloud.get_interface("KstoreMedia")
        self.store_media = kritter.SaveMediaQueue(path=MEDIA_DIR, keep=self.config_consts.IMAGES_KEEP, keep_uploaded=self.config_consts.IMAGES_KEEP)
        if self.config['gphoto_upload']:
            self.store_media.store_media = self.gphoto_interface 
        self.tracker = kritter.DetectionTracker(maxDisappeared=1, maxDistance=400, classSwitch=True)
        self.picker = kritter.DetectionPicker(timeout=self.config_consts.PICKER_TIMEOUT)
        self.detector_process = kritter.Processify(BirdInference)
        self.detector = kritter.KimageDetectorThread(self.detector_process)
        if self.config['enabled_classes'] is None:
            self.config['enabled_classes'] = self.detector_process.classes()
        self.set_threshold(self.config['detection_threshold']/100)

        style = {"label_width": 3, "control_width": 6}
        dstyle = {"label_width": 5, "control_width": 4}

        # Create video component and histogram enable.
        self.video = kritter.Kvideo(width=STREAM_WIDTH, overlay=True)
        brightness = kritter.Kslider(name="Brightness", value=self.camera.brightness, mxs=(0, 100, 1), format=lambda val: f'{val}%', style=style)
        self.video_c = kritter.Kbutton(name=[kritter.Kritter.icon("video-camera"), "Take video"], spinner=True)
        
        self.images_div = html.Div(self.create_images(), id=self.kapp.new_id(), style={"white-space": "nowrap", "max-width": f"{STREAM_WIDTH}px", "width": "100%", "overflow-x": "auto"})
        threshold = kritter.Kslider(name="Detection threshold", value=self.config['detection_threshold'], mxs=(MIN_THRESHOLD*100, MAX_THRESHOLD*100, 1), format=lambda val: f'{int(val)}%', style=dstyle)
        enabled_classes = kritter.Kchecklist(name="Enabled classes", options=self.detector_process.classes(), value=self.config['enabled_classes'], clear_check_all=True, scrollable=True, style=dstyle)
        trigger_classes = kritter.Kchecklist(name="Trigger classes", options=self.config['enabled_classes'], value=self.config['trigger_classes'], clear_check_all=True, scrollable=True, style=dstyle)
        upload = kritter.Kcheckbox(name="Upload to Google Photos", value=self.config['gphoto_upload'] and self.gphoto_interface is not None, disabled=self.gphoto_interface is None, style=dstyle)
        settings_button = kritter.Kbutton(name=[kritter.Kritter.icon("gear"), "Settings"], service=None)

        dlayout = [threshold, enabled_classes, trigger_classes, upload]
        settings = kritter.Kdialog(title=[kritter.Kritter.icon("gear"), "Settings"], layout=dlayout)
        controls = html.Div([brightness, self.video_c, settings_button])

        self.dialog_image = kritter.Kimage(overlay=True)
        self.image_dialog = kritter.Kdialog(title="", layout=[self.dialog_image], size="xl")
        self.dialog_video = kritter.Kvideo(src="")
        self.video_dialog = kritter.Kdialog(title="", layout=[self.dialog_video], size="xl")

        # Add video component and controls to layout.
        self.kapp.layout = html.Div([html.Div([self.video, self.images_div]), controls, settings, self.image_dialog, self.video_dialog], style={"padding": "15px"})
        self.kapp.push_mods(self.out_images())

        @brightness.callback()
        def func(value):
            self.config['brightness'] = value
            self.camera.brightness = value
            self.config.save()

        @self.video_c.callback()
        def func():
            if self.record_state==SAVING:
                return
            else:
                with self.lock:
                    self.record_state += 1
                    return self._update_record()

        @threshold.callback()
        def func(value):
            self.config['detection_threshold'] = value
            self.set_threshold(value/100) 
            self.config.save()

        @enabled_classes.callback()
        def func(value):
            # value list comes in unsorted -- let's sort to make it more human-readable
            value.sort(key=lambda c: c.lower())
            self.config['enabled_classes'] = value
            # Find trigger classes that are part of enabled classes            
            self.config['trigger_classes'] = [c for c in self.config['trigger_classes'] if c in value]
            self.config.save()
            return trigger_classes.out_options(value) + trigger_classes.out_value(self.config['trigger_classes'])

        @trigger_classes.callback()
        def func(value):
            self.config['trigger_classes'] = value
            self.config.save()

        @upload.callback()
        def func(value):
            self.config['gphoto_upload'] = value  
            self.store_media.store_media = self.gphoto_interface if value else None
            self.config.save()

        @settings_button.callback()
        def func():
            return settings.out_open(True)

        # Run camera grab thread.
        self.run_thread = True
        self._grab_thread = Thread(target=self.grab_thread)
        self._grab_thread.start()

        # Run Kritter server, which blocks.
        self.kapp.run()
        self.run_thread = False
        self._grab_thread.join()
        self.detector.close()
        self.detector_process.close()
        self.store_media.close()

    def create_images(self):
        children = []
        self.images = []
        for i in range(self.config_consts.IMAGES_DISPLAY):
            kimage = kritter.Kimage(width=self.config_consts.MARQUEE_IMAGE_WIDTH, overlay=True, style={"display": "inline-block", "margin": "5px 5px 5px 0"}, service=None)
            self.images.append(kimage)
            div = html.Div(kimage.layout, id=self.kapp.new_id(), style={"display": "inline-block"})
            
            def func(i):
                def func_():
                    path = self.images[i].path
                    if path.endswith(".mp4"):
                        return self.dialog_video.out_src(path) + self.video_dialog.out_title(f"Video, {self.images[i].data['timestamp']}") + self.video_dialog.out_open(True)
                    else:
                        return self.dialog_image.out_src(path) + self.image_dialog.out_title(f"{self.images[i].data['class']}, {self.images[i].data['timestamp']}") + self.image_dialog.out_open(True)
                     
                return func_

            kimage.callback()(func(i))
            children.append(div)
        return children

    def set_threshold(self, threshold):
        self.tracker.setThreshold(threshold)
        self.low_threshold = threshold - THRESHOLD_HYSTERESIS
        if self.low_threshold<MIN_THRESHOLD:
            self.low_threshold = MIN_THRESHOLD 

    # Frame grabbing thread
    def grab_thread(self):
        while self.run_thread:
            # Get frame
            frame = self.stream.frame()[0]
            # Get raw detections from detector thread
            detect = self.detector.detect(frame, self.low_threshold)
            if detect is not None:
                dets, det_frame = detect
                # Remove classes that aren't active
                #dets = self._filter_dets(dets)
                # Feed detections into tracker
                dets = self.tracker.update(dets, showDisappeared=True)
                # Render tracked detections to overlay
                mods = kritter.render_detected(self.video.overlay, dets)
                # Update picker
                mods += self.handle_picks(det_frame, dets)
                self.kapp.push_mods(mods)

            # Send frame
            self.video.push_frame(frame)

            # Handle manual video
            self._handle_record()            

    def _timestamp(self):
        return datetime.datetime.now().strftime("%a %H:%M:%S")

    def handle_picks(self, frame, dets):
        picks = self.picker.update(frame, dets)
        if picks:
            for i in picks:
                image, data = i[0], i[1]
                # Save picture and metadata, add width and height of image to data so we don't
                # need to decode it to set overlay dimensions.
                timestamp = self._timestamp()
                self.store_media.store_image_array(image, album=self.config_consts.GPHOTO_ALBUM, data={**data, 'width': image.shape[1], 'height': image.shape[0], "timestamp": timestamp})
                if data['class'] in self.config['trigger_classes']:
                    event = {**data, 'image': image, 'event_type': 'trigger', "timestamp": timestamp}
                    handle_event(self, event)

            return self.out_images()
        return []       

    def _filter_dets(self, dets):
        dets = [det for det in dets if det['class'] in self.config['enabled_classes']]
        return dets

    def out_images(self):
        images = os.listdir(MEDIA_DIR)
        images = [i for i in images if i.endswith(".jpg") or i.endswith(".mp4")]
        images.sort(reverse=True)
        images = images[0:self.config_consts.IMAGES_DISPLAY]
        mods = []
        for i in range(self.config_consts.IMAGES_DISPLAY):
            if i < len(images):
                image = images[i]
                data = self.store_media.load_metadata(os.path.join(MEDIA_DIR, image))
                self.images[i].path = image
                self.images[i].data = data
                self.images[i].overlay.draw_clear()
                if image.endswith(".mp4"):
                    image = data['thumbnail']

                mods += self.images[i].out_src(image)
                try:
                    mods += self.images[i].overlay.update_resolution(width=data['width'], height=data['height'])
                    if 'class' in data:
                        kritter.render_detected(self.images[i].overlay, [data], scale=self.config_consts.MARQUEE_IMAGE_WIDTH/1920)
                    else:
                        # create play arrow in overlay
                        ARROW_WIDTH = 0.18
                        ARROW_HEIGHT = ARROW_WIDTH*1.5
                        xoffset0 = (1-ARROW_WIDTH)*data['width']/2
                        xoffset1 = xoffset0 + ARROW_WIDTH*data['width']
                        yoffset0 = (data['height'] - ARROW_HEIGHT*data['width'])/2
                        yoffset1 = yoffset0 + ARROW_HEIGHT*data['width']/2
                        yoffset2 = yoffset1 + ARROW_HEIGHT*data['width']/2
                        points = [(xoffset0, yoffset0), (xoffset0, yoffset2), (xoffset1, yoffset1)]
                        self.images[i].overlay.draw_shape(points, fillcolor="rgba(255,255,255,0.85)", line={"width": 0})
                    self.images[i].overlay.draw_text(0, data['height']-1, data['timestamp'], fillcolor="black", font=dict(family="sans-serif", size=12, color="white"), xanchor="left", yanchor="bottom")
                    mods += self.images[i].overlay.out_draw() + self.images[i].out_disp(True)
                except:
                    pass
            else:
                mods += self.images[i].out_disp(False)
        return mods

    def _update_progress(self, percentage):
        if self.record_state==SAVING:
            self.kapp.push_mods(self.video_c.out_name([kritter.Kritter.icon("video-camera"), f"Saving... {percentage}%"]))

    def _save_video(self):
        self.store_media.store_video_stream(self.record, fps=self.camera.framerate, album=self.config_consts.GPHOTO_ALBUM, desc="Manual video", data={'width': self.camera.resolution[0], 'height': self.camera.resolution[1], "timestamp": self._timestamp()}, thumbnail=True, progress_callback=self._update_progress)
        self.record = None # free up memory, indicate that we're done.
        self.kapp.push_mods(self.out_images())

    def _update_record(self, stop=True):
        with self.lock:
            if self.record_state==WAITING:
                return self.video_c.out_name([kritter.Kritter.icon("video-camera"), "Take video"])+self.video_c.out_spinner_disp(False)
            elif self.record_state==RECORDING:
                # Record, save, encode simultaneously
                self.record = self.camera.record()
                self.save_thread = Thread(target=self._save_video)
                self.save_thread.start()
                return self.video_c.out_name([kritter.Kritter.icon("video-camera"), "Stop video"])+self.video_c.out_spinner_disp(True, disable=False)
            elif self.record_state==SAVING:
                if stop:
                    self.record.stop()
                return self.video_c.out_name([kritter.Kritter.icon("video-camera"), "Saving..."])+self.video_c.out_spinner_disp(True)

    def _handle_record(self):
        with self.lock:
            if self.record_state==RECORDING:
                if not self.record.recording():
                    self.record_state = SAVING
                    self.kapp.push_mods(self._update_record())
            elif self.record_state==SAVING:
                if not self.save_thread.is_alive():
                    self.record_state = WAITING
                    self.kapp.push_mods(self._update_record())

if __name__ == "__main__":
    Birdfeeder()

