import getpass
import grp
import logging
import os
import time

import bluesky.plan_stubs as bps

logger = logging.getLogger(__name__)


def zebra_daq_prep(zebra):
    yield from bps.mv(zebra.reset, 1)
    yield from bps.sleep(2.0)
    yield from bps.mv(
        zebra.out1, 31, zebra.m1_set_pos, 1, zebra.m2_set_pos, 1, zebra.m3_set_pos, 1, zebra.pc.arm_sel, 1,
    )


def setup_zebra_vector_scan(
    zebra,
    angle_start,
    gate_width,
    scan_width,
    pulse_width,
    pulse_step,
    exposure_period_per_image,
    num_images,
    is_still=False,
):
    yield from bps.mv(zebra.pc.gate.sel, angle_start)
    if is_still is False:
        yield from bps.mv(zebra.pc.gate.width, gate_width, zebra.pc.gate.step, scan_width)
    yield from bps.mv(
        zebra.pc.gate.num_gates,
        1,
        zebra.pc.pulse.start,
        0,
        zebra.pc.pulse.width,
        pulse_width,
        zebra.pc.pulse.step,
        pulse_step,
        zebra.pc.pulse.delay,
        exposure_period_per_image / 2 * 1000,
        zebra.pc.pulse.max,
        num_images,
    )


def setup_zebra_vector_scan_for_raster(
    zebra,
    angle_start,
    image_width,
    exposure_time_per_image,
    exposure_period_per_image,
    detector_dead_time,
    num_images,
    scan_encoder=3,
):
    yield from bps.mv(zebra.pc.encoder, scan_encoder)
    yield from bps.sleep(1.0)
    yield from bps.mv(zebra.pc.direction, 0, zebra.pc.gate.sel, 0)  # direction, 0 = positive
    yield from bps.mv(zebra.pc.gate.start, angle_start)
    if image_width != 0:
        yield from bps.mv(
            zebra.pc.gate.width, num_images * image_width, zebra.pc.gate.step, num_images * image_width + 0.01,
        )
    yield from bps.mv(
        zebra.pc.gate.num_gates,
        1,
        zebra.pc.pulse.sel,
        1,
        zebra.pc.pulse.start,
        0,
        zebra.pc.pulse.width,
        (exposure_time_per_image - detector_dead_time) * 1000,
        zebra.pc.pulse.step,
        exposure_period_per_image * 1000,
        zebra.pc.pulse.delay,
        exposure_period_per_image / 2 * 1000,
    )


def setup_vector_program(vector, num_images, angle_start, angle_end, exposure_period_per_image):
    yield from bps.mv(
        vector.num_frames,
        num_images,
        vector.start.omega,
        angle_start,
        vector.end.omega,
        angle_end,
        vector.frame_exptime,
        exposure_period_per_image * 1000.0,
        vector.hold,
        0,
    )


def setup_eiger_exposure(eiger, exposure_time, exposure_period):
    yield from bps.mv(eiger.acquire_time(exposure_time))
    yield from bps.mv(eiger.acquire_period(exposure_period))


def setup_eiger_triggers(eiger, mode, num_triggers, exposure_per_image):
    yield from bps.mv(eiger.trigger_mode, mode)
    yield from bps.mv(eiger.num_triggers, num_triggers)
    yield from bps.mv(eiger.trigger_exposure, exposure_per_image)


def setup_eiger_arming(
    eiger,
    start,
    width,
    num_images,
    exposure_per_image,
    file_prefix,
    data_directory_name,
    file_number_start,
    x_beam,
    y_beam,
    wavelength,
    det_distance_m,
):
    yield from bps.mv(eiger.save_files, 1)
    yield from bps.mv(eiger.file_owner, getpass.getuser())
    yield from bps.mv(eiger.file_owner_grp, grp.getgrgid(os.getgid())[0])
    yield from bps.mv(eiger.file_perms, 420)
    file_prefix_minus_directory = str(file_prefix)
    file_prefix_minus_directory = file_prefix_minus_directory.split('/')[-1]

    yield from bps.mv(eiger.acquire_time, exposure_per_image)
    yield from bps.mv(eiger.acquire_period, exposure_per_image)
    yield from bps.mv(eiger.num_images, num_images)
    yield from bps.mv(eiger.file_path, data_directory_name)
    yield from bps.mv(eiger.fw_name_pattern, f"{file_prefix_minus_directory}_$id")
    yield from bps.mv(eiger.sequence_id, file_number_start)

    # originally from detector_set_fileheader
    yield from bps.mv(eiger.beam_center_x, x_beam)
    yield from bps.mv(eiger.beam_center_y, y_beam)
    yield from bps.mv(eiger.omega_incr, width)
    yield from bps.mv(eiger.omega_start, start)
    yield from bps.mv(eiger.wavelength, wavelength)
    yield from bps.mv(eiger.det_distance, det_distance_m)

    start_arm = time.time()
    yield from bps.mv(eiger.acquire, 1)
    logger.info(f"arm time = {time.time() - start_arm}")


def setup_eiger_stop_acquire_and_wait(eiger):
    yield from bps.mv(eiger.acquire, 0)
    # wait until Acquire_RBV is 0
