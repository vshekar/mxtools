import getpass
import grp
import logging
import os
import time as ttime
from collections import deque

import h5py
from ophyd.sim import NullStatus
from ophyd.status import SubscriptionStatus

from . import eiger

logger = logging.getLogger(__name__)
DEFAULT_DATUM_DICT = {"data": None, "omega": None}


class MXFlyer:
    def __init__(self, vector, zebra, detector=None) -> None:
        self.name = "MXFlyer"
        self.vector = vector
        self.zebra = zebra
        self.detector = detector

        self._asset_docs_cache = deque()
        self._resource_uids = []
        self._datum_counter = None
        self._datum_ids = DEFAULT_DATUM_DICT
        self._master_file = None
        self._master_metadata = []

        self._collection_dictionary = None

    def read_configuration(self):
        return {}

    def describe_configuration(self):
        return {}

    def kickoff(self):
        self.detector.stage()
        self.vector.go.put(1)

        return NullStatus()

    def complete(self):
        def callback_motion(value, old_value, **kwargs):
            print(f"old: {old_value} -> new: {value}")
            if old_value == 1 and value == 0:
                return True
            else:
                return False

        motion_status = SubscriptionStatus(self.vector.active, callback_motion, run=False)
        # as an alternative, consider using self.zebra.download_status as the zebra should
        # finish after the vector has finished its movement.
        return motion_status

    def describe_collect(self):
        return_dict = {}
        return_dict["primary"] = {
            f"{self.detector.name}_image": {
                "source": f"{self.detector.name}_data",
                "dtype": "array",
                "shape": [
                    self.detector.cam.num_images.get(),
                    self.detector.cam.array_size.array_size_y.get(),
                    self.detector.cam.array_size.array_size_x.get(),
                ],
                "dims": ["images", "row", "column"],
                "external": "FILESTORE:",
            },
            "omega": {
                "source": f"{self.detector.name}_omega",
                "dtype": "array",
                "shape": [self.detector.cam.num_images.get()],
                "dims": ["images"],
                "external": "FILESTORE:",
            },
        }
        return return_dict

    def collect(self):
        self.unstage()

        now = ttime.time()
        self._master_metadata = self._extract_metadata()
        data = {f"{self.detector.name}_image": self._datum_ids["data"], "omega": self._datum_ids["omega"]}
        yield {
            "data": data,
            "timestamps": {key: now for key in data},
            "time": now,
            "filled": {key: False for key in data},
        }

    def collect_asset_docs(self):
        if self.detector:
            return self.detector.collect_asset_docs()
        return {}

    def _extract_metadata(self, field="omega"):
        with h5py.File(self._master_file, "r") as hf:
            return hf.get(f"entry/sample/goniometer/{field}")[()]

    def unstage(self):
        ttime.sleep(1.0)
        self.detector.unstage()

    def update_parameters(self, *args, **kwargs):
        self.configure_detector(**kwargs)
        self.configure_vector(**kwargs)
        self.configure_zebra(**kwargs)

    def configure_detector(self, **kwargs):
        file_prefix = kwargs["file_prefix"]
        data_directory_name = kwargs["data_directory_name"]
        self.detector.file.external_name.put(file_prefix)
        self.detector.file.write_path_template = data_directory_name

    def configure_vector(self, *args, **kwargs):
        angle_start = kwargs["angle_start"]
        scanWidth = kwargs["scan_width"]
        imgWidth = kwargs["img_width"]
        exposurePeriodPerImage = kwargs["exposure_period_per_image"]
        protocol = kwargs.get("protocol", "standard")
        x_um = (kwargs["x_start_um"], kwargs["x_end_um"])
        y_um = (kwargs["y_start_um"], kwargs["y_end_um"])
        z_um = (kwargs["z_start_um"], kwargs["z_end_um"])

        # scan encoder 0=x, 1=y,2=z,3=omega

        self.vector.sync.put(1)
        self.vector.expose.put(1)

        if imgWidth == 0:
            angle_end = angle_start
            numImages = scanWidth
        else:
            angle_end = angle_start + scanWidth
            numImages = int(round(scanWidth / imgWidth))
        total_exposure_time = exposurePeriodPerImage * numImages
        if total_exposure_time < 1.0 and protocol != "raster":
            self.vector.buffer_time.put(1000)
        else:
            self.vector.buffer_time.put(3)
        self.setup_vector_program(
            num_images=numImages,
            angle_start=angle_start,
            angle_end=angle_end,
            x_um=x_um,
            y_um=y_um,
            z_um=z_um,
            exposure_period_per_image=exposurePeriodPerImage,
        )

    def configure_zebra(self, *args, **kwargs):
        angle_start = kwargs["angle_start"]
        exposurePeriodPerImage = kwargs["exposure_period_per_image"]
        detector_dead_time = kwargs["detector_dead_time"]
        scanWidth = kwargs["scan_width"]
        imgWidth = kwargs["img_width"]
        numImages = kwargs["num_images"]
        self.zebra_daq_prep()
        ttime.sleep(0.5)

        PW = (exposurePeriodPerImage - detector_dead_time) * 1000
        PS = (exposurePeriodPerImage) * 1000
        GW = scanWidth - (1.0 - (PW / PS)) * (imgWidth / 2.0)
        self.setup_zebra_vector_scan(
            angle_start=angle_start,
            gate_width=GW,
            scan_width=scanWidth + 0.001,  # JA not sure why, but done in old LSDC
            pulse_width=PW,
            pulse_step=PS,
            exposure_period_per_image=exposurePeriodPerImage,
            num_images=numImages,
            is_still=imgWidth == 0,
        )

    def detector_arm(self, **kwargs):
        return self._arm_detector(
            start=kwargs["angle_start"],
            width=kwargs["img_width"],
            exposure_per_image=kwargs["exposure_period_per_image"],
            file_prefix=kwargs["file_prefix"],
            data_directory_name=kwargs["data_directory_name"],
            file_number_start=kwargs["file_number_start"],
            x_beam=kwargs["x_beam"],
            y_beam=kwargs["y_beam"],
            wavelength=kwargs["wavelength"],
            det_distance_m=kwargs["det_distance_m"],
            trigger_mode=eiger.EXTERNAL_SERIES,
            num_triggers=1,
            num_images=kwargs["num_images"],
            num_images_per_file=500,
        )

    def _arm_detector(
        self,
        *,
        start,
        width,
        exposure_per_image,
        file_prefix,
        data_directory_name,
        file_number_start,
        x_beam,
        y_beam,
        wavelength,
        det_distance_m,
        trigger_mode,
        num_triggers,
        num_images=None,
        num_images_per_file=500,
    ):

        self.detector.cam.save_files.put(1)
        self.detector.cam.file_owner.put(getpass.getuser())
        self.detector.cam.file_owner_grp.put(grp.getgrgid(os.getgid())[0])
        self.detector.cam.file_perms.put(420)
        file_prefix_minus_directory = str(file_prefix)
        file_prefix_minus_directory = file_prefix_minus_directory.split("/")[-1]

        self.detector.cam.acquire_time.put(exposure_per_image)
        self.detector.cam.acquire_period.put(exposure_per_image)
        # Trigger mode set before num_images due to updates in Eiger REST API
        self.detector.cam.trigger_mode.put(trigger_mode)
        if num_images is not None:
            self.detector.cam.num_images.put(num_images)
        self.detector.cam.num_triggers.put(num_triggers)
        self.detector.cam.file_path.put(data_directory_name)
        self.detector.cam.fw_name_pattern.put(f"{file_prefix_minus_directory}_$id")

        self.detector.cam.sequence_id.put(file_number_start)

        # originally from detector_set_fileheader
        self.detector.cam.beam_center_x.put(x_beam)
        self.detector.cam.beam_center_y.put(y_beam)
        self.detector.cam.omega_incr.put(width)
        self.detector.cam.omega_start.put(start)
        self.detector.cam.wavelength.put(wavelength)
        self.detector.cam.det_distance.put(det_distance_m)

        self.detector.file.file_write_images_per_file.put(num_images_per_file)

        def armed_callback(value, old_value, **kwargs):
            if old_value == 0 and value == 1:
                return True
            return False

        status = SubscriptionStatus(self.detector.cam.armed, armed_callback, run=False)

        self.detector.cam.acquire.put(1)

        return status

    def setup_vector_program(
        self, num_images, angle_start, angle_end, x_um, y_um, z_um, exposure_period_per_image
    ):
        self.vector.num_frames.put(num_images)
        self.vector.start.omega.put(angle_start)
        self.vector.end.omega.put(angle_end)
        self.vector.start.x.put(x_um[0])
        self.vector.end.x.put(x_um[1])
        self.vector.start.y.put(y_um[0])
        self.vector.end.y.put(y_um[1])
        self.vector.start.z.put(z_um[0])
        self.vector.end.z.put(z_um[1])
        self.vector.frame_exptime.put(exposure_period_per_image * 1000.0)
        self.vector.hold.put(0)

    def zebra_daq_prep(self):
        self.zebra.reset.put(1)
        ttime.sleep(0.5)  # not known why this sleep is so long (done since LSDC 1)
        self.zebra.out1.put(31)
        self.zebra.m1_set_pos.put(1)
        self.zebra.m2_set_pos.put(1)
        self.zebra.m3_set_pos.put(1)
        self.zebra.pc.arm.trig_source.put(1)

    # expected zebra setup:
    #     time in ms
    #     Posn direction: positive
    #     gate trig source - Position
    #     pulse trig source - Time
    def setup_zebra_vector_scan(
        self,
        angle_start,
        gate_width,
        scan_width,
        pulse_width,
        pulse_step,
        exposure_period_per_image,
        num_images,
        is_still=False,
    ):
        self.zebra.pc.gate.start.put(angle_start, wait=True)
        if is_still is False:
            self.zebra.pc.gate.width.put(gate_width, wait=True)
            self.zebra.pc.gate.step.put(scan_width, wait=True)
        self.zebra.pc.gate.num_gates.put(1, wait=True)
        self.zebra.pc.pulse.start.put(0, wait=True)
        self.zebra.pc.pulse.width.put(pulse_width, wait=True)
        self.zebra.pc.pulse.step.put(pulse_step, wait=True)
        self.zebra.pc.pulse.delay.put(exposure_period_per_image / 2 * 1000, wait=True)
        self.zebra.pc.pulse.max.put(num_images, wait=True)
