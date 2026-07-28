import logging
import os
import time as ttime
import uuid

from ophyd.sim import NullStatus

from . import eiger
from .flyer import MXFlyer

logger = logging.getLogger(__name__)


class MXRasterFlyer(MXFlyer):
    def __init__(self, vector, zebra, detector) -> None:
        self.name = "MXRasterFlyer"
        super().__init__(vector, zebra, detector)
        # Raster arms the detector once for the whole scan (one master .h5),
        # but the RunEngine calls collect_asset_docs once per row. A single
        # Resource is emitted on row 0 and cached in _resource_uid; subsequent
        # rows emit only a Datum referencing that Resource.
        self._staged_resource = None
        self._resource_uid = None
        self._row_index = 0
        self._row_positions = {}
        self._num_images = None

    def kickoff(self):
        # ttime.sleep(0.2)  # TODO see if vector starts ok without this sleep
        self.vector.go.put(1)
        return NullStatus()

    def update_parameters(self, *args, **kwargs):
        logger.debug("starting updating parameters")
        self.configure_vector(**kwargs)
        row_index = kwargs.get("row_index", 0)
        self._row_index = row_index
        self._num_images = kwargs["num_images"]
        self._row_positions = {
            "x_start": kwargs["x_start_um"],
            "x_end": kwargs["x_end_um"],
            "y_start": kwargs["y_start_um"],
            "y_end": kwargs["y_end_um"],
            "z_start": kwargs["z_start_um"],
            "z_end": kwargs["z_end_um"],
        }
        if row_index == 0:
            logger.debug("row 0: fully configuring zebra")
            # New scan: force re-reading the staged Resource on the next
            # collect_asset_docs call.
            self._staged_resource = None
            self._resource_uid = None
            self.configure_zebra(**kwargs)
        else:
            numImages = kwargs["num_images"]
            logger.debug(f"row {row_index}: only setting pulse max")
            self.zebra.pc.pulse.max.put(numImages)
        logger.debug("finished updating parameters")

    def configure_zebra(self, **kwargs):
        angle_start = kwargs["angle_start"]
        exposurePeriodPerImage = kwargs["exposure_period_per_image"]
        detector_dead_time = kwargs["detector_dead_time"]
        scanWidth = kwargs["scan_width"]
        imgWidth = kwargs["img_width"]
        numImages = kwargs["num_images"]
        self.zebra_daq_prep()
        self.zebra.pc.encoder.put(3)  # encoder 0=x, 1=y,2=z,3=omega
        ttime.sleep(0.5)  # used since LSDC 1 - reason unknown
        self.zebra.pc.direction.put(0)  # direction 0 = positive
        self.zebra.pc.gate.sel.put(0)
        self.zebra.pc.pulse.sel.put(1)
        self.zebra.pc.pulse.start.put(0)

        PW = (exposurePeriodPerImage - 2 * detector_dead_time) * 1000
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
            logger.debug(f"before: gate width: {gate_width} gate step: {scan_width}")
            self.zebra.pc.gate.width.put(gate_width, wait=True)
            self.zebra.pc.gate.step.put(scan_width, wait=True)
        self.zebra.pc.gate.num_gates.put(1, wait=True)
        self.zebra.pc.pulse.start.put(0, wait=True)
        logger.debug(f"before: pulse width: {pulse_width}")
        self.zebra.pc.pulse.width.put(pulse_width, wait=True)
        self.zebra.pc.pulse.step.put(pulse_step, wait=True)
        logger.debug(f"before: pulse delay: {exposure_period_per_image / 2 * 1000}")
        self.zebra.pc.pulse.delay.put(exposure_period_per_image / 2 * 1000, wait=True)
        logger.debug(
            f"after: gate width: {self.zebra.pc.gate.width.get()} gate step: {self.zebra.pc.gate.step.get()}"
            f"after: pulse width: {self.zebra.pc.pulse.width.get()} pulse delay: {self.zebra.pc.pulse.delay.get()}"
        )
        self.zebra.pc.pulse.max.put(num_images, wait=True)
        self.vector.hold.put(0)  # necessary to prevent problems upon
        # exposure time change

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
            trigger_mode=eiger.EXTERNAL_ENABLE,
            num_triggers=kwargs["total_num_images"],
            num_images=None,
            num_images_per_file=kwargs["num_images_per_file"],
        )

    def collect_asset_docs(self):
        """Emit one Resource (row 0 only) + one Datum per raster row.

        The detector is armed once for the whole raster, producing a single
        master ``.h5`` whose ``entry/data`` group holds one linked dataset per
        row (``data_000001``, ``data_000002``, ...).

        A single StreamResource is emitted on row 0 covering the entire scan
        (``dataset="entry/data"``, ``multiplier=num_images``,
        ``join_method="stack"``).  Every row then emits one StreamDatum with
        ``indices={start: row_index, stop: row_index+1}``, so the consolidated
        shape in Tiled is ``(num_rows, num_images, Y, X)``.
        """
        detector = self.detector

        # Row 0: drain the ophyd FileStore cache and build the master-file path.
        if self._staged_resource is None:
            ((_, staged_resource),) = detector.file.collect_asset_docs()
            root = staged_resource["root"]
            resource_path = staged_resource["resource_path"]
            seq_id = int(detector.cam.sequence_id.get())
            master_file = f"{root}/{resource_path}_{seq_id}_master.h5"
            if not os.path.isfile(master_file):
                raise RuntimeError(f"File {master_file} does not exist")
            self._staged_resource = {
                "seq_id": seq_id,
                "master_file": master_file,
            }

        seq_id = self._staged_resource["seq_id"]
        master_file = self._staged_resource["master_file"]
        detector._master_file = master_file

        num_images = self._num_images
        if num_images is None:
            num_images = int(detector.cam.num_images.get())

        docs = []

        # Emit the Resource document exactly once (row 0).
        if self._resource_uid is None:
            self._resource_uid = str(uuid.uuid4())
            resource_doc = {
                "uid": self._resource_uid,
                "spec": "AD_EIGER_MX_RASTER",
                "root": "",
                "resource_path": master_file,
                "resource_kwargs": {
                    "seq_id": seq_id,
                    "dataset": "entry/data",
                    "multiplier": num_images,
                    "join_method": "stack",
                },
                "path_semantics": "posix",
            }
            docs.append(("resource", resource_doc))

        # One Datum per row: index i → row i of the raster.
        datum_id = f"{self._resource_uid}/{self._row_index}"
        detector._datum_ids["data"] = datum_id
        detector._datum_ids["omega"] = None

        docs.append((
            "datum",
            {
                "resource": self._resource_uid,
                "datum_id": datum_id,
                "datum_kwargs": {
                    "data_key": "data",
                    "indices": {"start": self._row_index, "stop": self._row_index + 1},
                },
            },
        ))

        return tuple(docs)

    def describe_collect(self):
        detector = self.detector
        # In EXTERNAL_ENABLE mode the detector is armed with num_images=None, so
        # cam.num_images does not reflect the per-row frame count. Use the value
        # supplied per row via update_parameters (number of steps in the row).
        num_images_per_row = self._num_images
        if num_images_per_row is None:
            num_images_per_row = detector.cam.num_images.get()
        # Return the FLAT {data_key: DataKey} mapping (no stream-name wrapper).
        # bps.declare_stream(collect=True) supplies the stream name itself; a
        # nested {"primary": {...}} return makes bluesky treat "primary" as a
        # data_key and inject object_name into it, failing descriptor validation.
        # num_images_per_row is no longer used in the image shape (per-frame
        # shape below), but is retained above for backward reference.
        _ = num_images_per_row
        return {
            f"{detector.name}_image": {
                "source": f"{detector.name}_data",
                "dtype": "array",
                "dtype_numpy": "<f8",
                # Per-frame shape. The row's frame count is inferred downstream
                # from the StreamDatum indices; the consolidator stacks frames
                # into (num_images_per_row, row, column).
                "shape": [
                    detector.cam.array_size.array_size_y.get(),
                    detector.cam.array_size.array_size_x.get(),
                ],
                "dims": ["row", "column"],
                "external": "FILESTORE:",
            },
            # Plan-supplied row start/end positions. These are not stored in the
            # master file and are not per-frame, so they stay as in-event scalars.
            "x_start": {"source": f"{self.name}_x_start", "dtype": "number", "dtype_numpy": "<f8", "shape": [], "dims": []},
            "x_end": {"source": f"{self.name}_x_end", "dtype": "number", "dtype_numpy": "<f8", "shape": [], "dims": []},
            "y_start": {"source": f"{self.name}_y_start", "dtype": "number", "dtype_numpy": "<f8", "shape": [], "dims": []},
            "y_end": {"source": f"{self.name}_y_end", "dtype": "number", "dtype_numpy": "<f8", "shape": [], "dims": []},
            "z_start": {"source": f"{self.name}_z_start", "dtype": "number", "dtype_numpy": "<f8", "shape": [], "dims": []},
            "z_end": {"source": f"{self.name}_z_end", "dtype": "number", "dtype_numpy": "<f8", "shape": [], "dims": []},
        }

    def collect(self):
        # Unlike the standard flyer we do NOT unstage here (the detector stays
        # armed across all rows and is unstaged once by the plan at the end) and
        # we do not read omega metadata from the master file.
        now = ttime.time()
        data = {
            f"{self.detector.name}_image": f"{self._resource_uid}/{self._row_index}",
            **self._row_positions,
        }
        yield {
            "data": data,
            "timestamps": {key: now for key in data},
            "time": now,
            "filled": {f"{self.detector.name}_image": False},
        }
