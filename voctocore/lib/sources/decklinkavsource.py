#!/usr/bin/env python3
import logging
import re

from gi.repository import Gst

from lib.config import Config
from lib.sources.avsource import AVSource


class DeckLinkAVSource(AVSource):

    def __init__(self, name, has_audio=True, has_video=True):
        self.log = logging.getLogger('DecklinkAVSource[{}]'.format(name))
        super().__init__(name, has_audio, has_video)

        section = 'source.{}'.format(name)

        # Device number, default: 0
        self.device = Config.get(section, 'devicenumber', fallback=0)

        # Audio connection, default: Automatic
        self.aconn = Config.get(section, 'audio_connection', fallback='auto')

        # Video connection, default: Automatic
        self.vconn = Config.get(section, 'video_connection', fallback='auto')

        # Video mode, default: 1080i50
        self.vmode = Config.get(section, 'video_mode', fallback='1080i50')

        # Video format, default: auto
        self.vfmt = Config.get(section, 'video_format', fallback='auto')

        self.audiostream_map = self._parse_audiostream_map(section)
        self.log.info("audiostream_map: %s", self.audiostream_map)

        self.fallback_default = False
        if len(self.audiostream_map) == 0:
            self.log.info("no audiostream-mapping defined,"
                          "defaulting to mapping channel 0+1 to first stream")
            self.fallback_default = True

        self._warn_incorrect_number_of_streams()

        self.required_input_channels = \
            self._calculate_required_input_channels()

        self.log.info("configuring decklink-input to %u channels",
                      self.required_input_channels)

        min_gst_multi_channels = (1, 12, 3)
        if self.required_input_channels > 2 and \
                Gst.version() < min_gst_multi_channels:

            self.log.warning(
                'GStreamer version %s is probably too to use more then 2 '
                'channels on your decklink source. officially supported '
                'since %s',
                tuple(Gst.version()), min_gst_multi_channels)

        self.launch_pipeline()

    def port(self):
        return "Decklink #{}".format(self.device)

    def audio_channels(self):
        if len(self.audiostream_map) == 0:
            return 1
        else:
            return len(self.audiostream_map)

    def _calculate_required_input_channels(self):
        required_input_channels = 0
        for audiostream, mapping in self.audiostream_map.items():
            left, right = self._parse_audiostream_mapping(mapping)
            required_input_channels = max(required_input_channels, left + 1)
            if right:
                required_input_channels = max(required_input_channels,
                                              right + 1)

        required_input_channels = \
            self._round_decklink_channels(required_input_channels)

        return required_input_channels

    def _round_decklink_channels(self, required_input_channels):
        if required_input_channels > 16:
            raise RuntimeError(
                "Decklink-Devices support up to 16 Channels,"
                "you requested {}".format(required_input_channels))

        elif required_input_channels > 8:
            required_input_channels = 16

        elif required_input_channels > 2:
            required_input_channels = 8

        else:
            required_input_channels = 2

        return required_input_channels

    def _parse_audiostream_map(self, config_section):
        audiostream_map = {}

        if config_section not in Config:
            return audiostream_map

        for key in Config[config_section]:
            value = Config.get(config_section, key)
            m = re.match(r'audiostream\[(\d+)\]', key)
            if m:
                audiostream = int(m.group(1))
                audiostream_map[audiostream] = value

        return audiostream_map

    def _parse_audiostream_mapping(self, mapping):
        m = re.match(r'(\d+)\+(\d+)', mapping)
        if m:
            return (int(m.group(1)), int(m.group(2)),)
        else:
            return (int(mapping), None,)

    def _warn_incorrect_number_of_streams(self):
        num_streams = Config.getint('mix', 'audiostreams')
        for audiostream, mapping in self.audiostream_map.items():
            if audiostream >= num_streams:
                raise RuntimeError(
                    "Mapping-Configuration for Stream 0 to {} found,"
                    "but only {} enabled"
                    .format(audiostream, num_streams))

    def __str__(self):
        return 'DecklinkAVSource[{name}] reading card #{device}'.format(
            name=self.name,
            device=self.device
        )

    def launch_pipeline(self):
        # A video source is required even when we only need audio
        pipeline = """
decklinkvideosrc
    device-number={device}
    connection={conn}
    video-format={fmt}
    mode={mode}
        """.format(
            device=self.device,
            conn=self.vconn,
            mode=self.vmode,
            fmt=self.vfmt
        )

        if self.has_video:
            pipeline += """
! {deinterlacer}

videoconvert
! videoscale
! videorate
    name=vout-{name}
            """.format(
                deinterlacer=self.build_deinterlacer(),
                name=self.name
            )
        else:
            pipeline += """
! fakesink
            """

        if self.has_audio:
            pipeline += """
decklinkaudiosrc
    {channels}
    device-number={device}
    connection={conn}
    {output}
            """.format(
                channels="channels={}".format(self.required_input_channels)
                         if self.required_input_channels > 2 else
                         "",
                device=self.device,
                conn=self.aconn,
                output="name=aout-{name}".format(name=self.name)
                       if self.fallback_default else
                       """
! deinterleave
    name=aout-{name}
                       """.format(name=self.name),
            )

            for audiostream, mapping in self.audiostream_map.items():
                left, right = self._parse_audiostream_mapping(mapping)
                if right is not None:
                    self.log.info(
                        "mapping decklink input-channels {left} and {right}"
                        "as left and right to output-stream {audiostream}"
                        .format(left=left,
                                right=right,
                                audiostream=audiostream))

                    pipeline += """
interleave
    name=i-{name}-{audiostream}

aout-{name}.src_{left}
! queue
    name=queue-decklink-audio-{name}-{audiostream}-left
! i-{name}-{audiostream}.sink_0

aout-{name}.src_{right}
! queue
    name=queue-decklink-audio-{name}-{audiostream}-right
! i-{name}-{audiostream}.sink_1
                    """.format(
                        left=left,
                        right=right,
                        name=self.name,
                        audiostream=audiostream
                    )
                else:
                    self.log.info(
                        "mapping decklink input-channel {channel} "
                        "as left and right to output-stream {audiostream}"
                        .format(channel=left,
                                audiostream=audiostream))

                    pipeline += """
interleave
    name=i-{name}-{audiostream}

aout-{name}.src_{channel}
! tee
    name=t-{name}-{audiostream}

t-{name}-{audiostream}.
! queue
    name=queue-decklink-audio-{name}-{audiostream}-left
! i-{name}-{audiostream}.sink_0

t-{name}-{audiostream}.
! queue
    name=queue-decklink-audio-{name}-{audiostream}-right
! i-{name}-{audiostream}.sink_1
                    """.format(
                        channel=left,
                        name=self.name,
                        audiostream=audiostream
                    )

        self.build_pipeline(pipeline)

    def build_deinterlacer(self):
        deinterlacer = super().build_deinterlacer()
        if deinterlacer:
            deinterlacer += ' !'
        else:
            deinterlacer = ''

        return deinterlacer

    def build_audioport(self, audiostream):
        if self.fallback_default and audiostream == 0:
            return "aout-{}.".format(self.name)

        if audiostream in self.audiostream_map:
            return 'i-{name}-{audiostream}.'.format(name=self.name, audiostream=audiostream)

    def build_videoport(self):
        return 'vout-{}.'.format(self.name)

    def restart(self):
        self.bin.set_state(Gst.State.NULL)
        self.launch_pipeline()
