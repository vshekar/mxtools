import logging
import pathlib

import dask.array as da
import h5py
from area_detector_handlers import HandlerBase

logger = logging.getLogger(__name__)


class EigerHandlerMX(HandlerBase):
    spec = "AD_EIGER_MX"

    def __init__(self, fpath, seq_id):
        self._seq_id = seq_id
        # The resource_path emitted by the device now already includes the
        # ``_{seq_id}_master.h5`` suffix, so ``fpath`` points directly at the
        # master file. Do NOT re-append the suffix here (that would produce
        # ``..._N_master.h5_N_master.h5``).
        #
        # NOTE: pre-migration archived runs emitted a bare resource_path and
        # will not resolve with this handler. They must be read via the legacy
        # path or a versioned handler.
        self._fpath = pathlib.Path(fpath).absolute()
        if not self._fpath.is_file():
            raise RuntimeError(f"File {self._fpath} does not exist")

        # print(f"Eiger master file: {self._fpath}")  # TODO make this a logging debug message

    def __call__(self, data_key="data", **kwargs):
        self._file = h5py.File(self._fpath, "r")  # but make it cached

        if data_key == "data":
            temp = []
            group = self._file["entry"]["data"]
            for i, k in enumerate(group):
                reta = da.from_array(group[k])
                logger.debug(f"{i} {reta.shape}")
                temp.append(reta)

            ret = da.stack(temp)
            newret = ret.reshape(-1, *ret.shape[-2:])
            logger.debug(f"{newret.shape}")
            return newret

        elif data_key == "omega":
            return da.from_array(self._file["entry"]["sample"]["goniometer"][data_key])

        elif data_key == "bit_mask":
            ...
            # code to pull out bit mask
            raise NotImplementedError()

        elif data_key in self._file["entry"]["instrument"]:
            return da.from_array(self._file["entry"]["instrument"][data_key])

        else:
            raise RuntimeError(f"Unknown key: {data_key}")
