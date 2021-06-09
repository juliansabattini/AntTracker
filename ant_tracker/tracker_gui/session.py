from dataclasses import field, dataclass
from enum import auto

import datetime
import json
from pathlib import Path
from typing import Tuple, Union, List, Dict, Optional

from .extracted_parameters import ExtractedParameters, SelectionStep
from ..tracker.common import ensure_path, to_json, SerializableEnum, crop_from_rect, filehash
from ..tracker.track import TrackId, Track
from ..tracker.tracking import Tracker

@dataclass
class SessionInfo:
    class State(SerializableEnum):
        New = auto()
        GotParameters = auto()
        Tracking = auto()
        DetectingLeaves = auto()
        Finished = auto()

        @staticmethod
        def __indexes() -> List['SessionInfo.State']:
            S = SessionInfo.State
            return [S.New, S.GotParameters, S.Tracking, S.DetectingLeaves, S.Finished]

        def __gt__(self, other: 'SessionInfo.State'):
            S = SessionInfo.State
            s = S.__indexes().index(self)
            o = S.__indexes().index(other)
            return s > o

        def __lt__(self, other: 'SessionInfo.State'):
            S = SessionInfo.State
            s = S.__indexes().index(self)
            o = S.__indexes().index(other)
            return s < o

        def __le__(self, other: 'SessionInfo.State'):
            return not (self > other)

        def __ge__(self, other: 'SessionInfo.State'):
            return not (self < other)

    videofiles: List[Path]
    first_start_time: datetime.datetime
    lengths: Dict[Path, Optional[int]]
    states: Dict[Path, State]
    parameters: Dict[Path, Optional[ExtractedParameters]]
    active_trackers: Dict[Path, Optional[Tracker]]
    unfinished_trackers: Dict[Path, Optional[Tuple[Path,Path]]]
    detection_probs: Dict[Path, Dict[TrackId, float]]
    save_every_n_frames: int = 1000
    __is_first_run: bool = field(init=False, default=False)

    @staticmethod
    def __sort(files: List[Path]):
        from natsort import natsorted
        return natsorted(files, key=lambda f: str(f))

    @classmethod
    def first_run(cls, files: List[Path], first_start_time: datetime.datetime):
        self = cls(
            videofiles=cls.__sort(files),
            first_start_time=first_start_time,
            lengths={f: None for f in files},
            states={f: SessionInfo.State.New for f in files},
            parameters={f: None for f in files},
            active_trackers={f: None for f in files},
            unfinished_trackers={f: None for f in files},
            detection_probs={f: {} for f in files},
        )
        self.__is_first_run = True
        return self

    @property
    def is_first_run(self):
        return self.__is_first_run

    @staticmethod
    def get_trkfile(videofile: Union[Path, str]):
        videofile = ensure_path(videofile)
        return videofile.parent / (videofile.stem + '.trk')

    def add_new_files(self, files: List[Path]):
        self.videofiles += files
        self.videofiles = self.__sort(self.videofiles)
        self.lengths = {**self.lengths, **{f: None for f in files}}
        self.states = {**self.states, **{f: SessionInfo.State.New for f in files}}
        self.parameters = {**self.parameters, **{f: None for f in files}}
        self.active_trackers = {**self.active_trackers, **{f: None for f in files}}
        self.unfinished_trackers = {**self.unfinished_trackers, **{f: None for f in files}}
        self.detection_probs = {**self.detection_probs, **{f: {} for f in files}}

    def remove_deleted_files(self, files: List[Path]):
        for file in files:
            self.videofiles.remove(file)
            del self.lengths[file]
            del self.states[file]
            del self.parameters[file]
            del self.active_trackers[file]
            del self.unfinished_trackers[file]
            del self.detection_probs[file]
        self.videofiles = self.__sort(self.videofiles)

    def record_tracker_state(self, file: Union[Path, str], tracker: Tracker):
        file = ensure_path(file)
        if file not in self.videofiles: raise ValueError(f"El archivo {file} no pertenece a esta sesión")
        if filehash(Path(tracker.video_path)) != filehash(file):
            raise ValueError(f"El archivo {file} no corresponde a este Tracker ({tracker.video_path})")
        if self.states[file] != SessionInfo.State.Tracking:
            raise ValueError(f"El archivo {file} no está actualmente en tracking (estado: {self.states[file]})")
        self.active_trackers[file] = tracker

    def record_detection(self, file: Union[Path, str], track: Track, prob: float):
        file = ensure_path(file)
        self.detection_probs[file][track.id] = prob

    def save(self, path: Union[Path, str]):
        path = ensure_path(path)
        for file in self.videofiles:
            if self.states[file] == SessionInfo.State.Tracking and self.active_trackers[file]:
                vname = self.active_trackers[file].video_path.name
                closed_file = path.parent / f".{vname}.uctrk"
                ongoing_file = path.parent / f".{vname}.uotrk"
                self.active_trackers[file].save_unfinished(closed_file, ongoing_file)
                self.unfinished_trackers[file] = (closed_file, ongoing_file)
            if self.states[file] > SessionInfo.State.Tracking and self.active_trackers[file]:
                vname = self.active_trackers[file].video_path.name
                (path.parent / f".{vname}.uctrk").unlink(True)
                (path.parent / f".{vname}.uotrk").unlink(True)
                self.active_trackers[file] = None
                self.unfinished_trackers[file] = None
            if self.states[file] != SessionInfo.State.DetectingLeaves and self.detection_probs[file]:
                self.detection_probs[file] = {}
        path.write_text(
            to_json({
                'videofiles':          [str(p.name) for p in self.videofiles],
                'first_start_time':    self.first_start_time.isoformat(),
                'lengths':             {str(p.name): l for p, l in self.lengths.items()},
                'states':              {str(p.name): s.name for p, s in self.states.items()},
                'parameters':          {str(p.name): (s.encode() if s is not None else None)
                                            for p, s in self.parameters.items()},
                'unfinished_trackers': {str(p.name): (ct[0].name, ct[1].name) if ct else None
                                            for p, ct in self.unfinished_trackers.items()},
                'detection_probs':     {str(p.name): {str(i): prob for i, prob in probs.items()} for p, probs in
                                        self.detection_probs.items()},
                'save_every_n_frames': self.save_every_n_frames,
            })
        )

    @classmethod
    def load(cls, path: Union[Path, str], load_active_trackers=False):
        path = ensure_path(path)
        d = json.loads(path.read_text())

        active_trackers = {(path.parent / p): None for p, _ in d['unfinished_trackers'].items()}
        unfinished_trackers = {
            (path.parent / p): ((path.parent / ct[0], path.parent / ct[1]) if ct else None)
                for p, ct in d['unfinished_trackers'].items()}
        for videofile, co in unfinished_trackers.items():
            co: Optional[Tuple[Path, Path]]
            if co is not None:
                closed_file, ongoing_file = co
                if load_active_trackers:
                    import pims
                    from pims.process import crop
                    crop_rect = (ExtractedParameters.decode(d['parameters'][videofile.name])
                                    .rect_data[SelectionStep.TrackingArea])
                    video = pims.PyAVReaderIndexed(videofile)
                    video = crop(video, crop_from_rect(video.frame_shape[0:2], crop_rect))
                    active_trackers[videofile] = Tracker.load_unfinished(
                        closed_file, ongoing_file, video, videofile)
        if 'detection_probs' in d:
            detection_probs = {(path.parent / p): {int(i): prob for i, prob in probs.items()} for p, probs in
                               d['detection_probs'].items()}
        else:
            detection_probs = {(path.parent / p): {} for p in d['videofiles']}

        self = cls(
            videofiles=[(path.parent / p) for p in d['videofiles']],
            first_start_time=datetime.datetime.fromisoformat(d['first_start_time']),
            lengths={(path.parent / p): l for p, l in d['lengths'].items()},
            states={(path.parent / p): SessionInfo.State[s] for p, s in d['states'].items()},
            parameters={(path.parent / p): (ExtractedParameters.decode(s) if s else None)
                for p, s in d['parameters'].items()},
            active_trackers=active_trackers,
            unfinished_trackers=unfinished_trackers,
            detection_probs=detection_probs,
            save_every_n_frames=d.get('save_every_n_frames',1000),
        )
        return self
