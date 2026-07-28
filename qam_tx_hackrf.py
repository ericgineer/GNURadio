#!/usr/bin/env python3
"""Transmit a QAM frame over a HackRF using GNU Radio.

The transmitter builds a repeating frame with:
  - 704-symbol preamble split into 64 AGC/Detect, 128 Sync/CFO, and 512 equalizer-training symbols
  - 4096-symbol payload consisting of 16 repeats of 255 data symbols plus one pilot symbol

The payload uses a deterministic PRBS sequence so BER can be measured later.

Run with:
  python qam_tx_hackrf.py --modulation qpsk --freq 433.92e6 --gain 20
"""

import argparse
import math

import numpy as np
from gnuradio import gr, blocks, filter
from gnuradio import soapy


class FrameSource(gr.sync_block):
    """Generate a repeating frame of complex QAM symbols for the selected modulation."""

    def __init__(self, modulation="qpsk", symbol_rate=1e6):
        gr.sync_block.__init__(
            self,
            name="frame_source",
            in_sig=[],
            out_sig=[np.complex64],
        )

        self.modulation = modulation.lower()
        self.symbol_rate = symbol_rate
        self.bits_per_symbol = {
            "bpsk": 1,
            "qpsk": 2,
            "16qam": 4,
            "64qam": 6,
        }[self.modulation]
        self.constellation = self._make_constellation()
        self.frame_symbols = self._build_frame()
        self.index = 0

    def _make_constellation(self):
        if self.modulation == "bpsk":
            return [(-1 + 0j), (1 + 0j)]
        if self.modulation == "qpsk":
            scale = 1.0 / math.sqrt(2.0)
            return [
                (-1 - 1j) * scale,
                (-1 + 1j) * scale,
                (1 + 1j) * scale,
                (1 - 1j) * scale,
            ]
        if self.modulation == "16qam":
            levels = [-3, -1, 1, 3]
            scale = 1.0 / math.sqrt(10.0)
            points = []
            for y in levels:
                for x in levels:
                    points.append((x + 1j * y) * scale)
            return points
        if self.modulation == "64qam":
            levels = [-7, -5, -3, -1, 1, 3, 5, 7]
            scale = 1.0 / math.sqrt(42.0)
            points = []
            for y in levels:
                for x in levels:
                    points.append((x + 1j * y) * scale)
            return points
        raise ValueError(f"Unsupported modulation: {self.modulation}")

    def _prbs_bits(self, count, seed):
        state = seed & 0xFFFF
        bits = []
        for _ in range(count):
            bit = ((state >> 15) ^ (state >> 13) ^ (state >> 4) ^ (state >> 1)) & 0x1
            state = ((state << 1) & 0xFFFF) | bit
            bits.append(bit)
        return bits

    def _bits_to_symbols(self, bits):
        symbols = []
        for offset in range(0, len(bits), self.bits_per_symbol):
            chunk = bits[offset:offset + self.bits_per_symbol]
            if len(chunk) < self.bits_per_symbol:
                chunk = chunk + [0] * (self.bits_per_symbol - len(chunk))
            value = 0
            for bit in chunk:
                value = (value << 1) | bit
            index = value % len(self.constellation)
            symbols.append(self.constellation[index])
        return symbols

    def _build_preamble(self, length):
        preamble = []
        section_lengths = [64, 128, 512]
        seeds = [0x1234, 0x2345, 0x3456]
        for section_len, seed in zip(section_lengths, seeds):
            bits = self._prbs_bits(section_len * self.bits_per_symbol, seed)
            symbols = self._bits_to_symbols(bits)
            preamble.extend(symbols[:section_len])
        return preamble[:length]

    def _build_payload(self, length):
        pilot_symbols = [1 + 1j, 1 - 1j, -1 - 1j, -1 + 1j]
        payload = []
        pilot_index = 0
        data_count = 0
        while len(payload) < length:
            for _ in range(255):
                bits = self._prbs_bits(self.bits_per_symbol, seed=0x4567 + data_count)
                symbols = self._bits_to_symbols(bits)
                payload.append(symbols[0])
                data_count += 1
            payload.append(pilot_symbols[pilot_index % 4])
            pilot_index += 1
        return payload[:length]

    def _build_frame(self):
        preamble = self._build_preamble(704)
        payload = self._build_payload(4096)
        return np.array(preamble + payload, dtype=np.complex64)

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        for i in range(n):
            out[i] = self.frame_symbols[self.index]
            self.index = (self.index + 1) % len(self.frame_symbols)
        return n


def build_top_block(args):
    tb = gr.top_block()

    sample_rate = max(float(args.sample_rate), 2e6)
    symbol_rate = min(float(args.symbol_rate), sample_rate / 4.0)
    sps = max(4, int(round(sample_rate / max(symbol_rate, 1.0))))

    frame_source = FrameSource(modulation=args.modulation, symbol_rate=symbol_rate)
    rrc_taps = filter.firdes.root_raised_cosine(
        gain=1.0,
        sampling_freq=sample_rate,
        symbol_rate=symbol_rate,
        alpha=0.35,
        ntaps=11 * sps,
    )
    pulse_shaper = filter.interp_fir_filter_ccf(sps, rrc_taps)
    gain_stage = blocks.multiply_const_cc(0.35)
    throttle = blocks.throttle(gr.sizeof_gr_complex, sample_rate, True)

    sink = soapy.sink("driver=hackrf", "fc32", 1, '', '', [''], [''])
    sink.set_sample_rate(0, sample_rate)
    sink.set_bandwidth(0, 0)
    sink.set_frequency(0, int(args.freq))
    sink.set_gain(0, 'AMP', False)
    sink.set_gain(0, 'VGA', min(max(float(args.gain), 0.0), 47.0))

    tb.connect(frame_source, pulse_shaper, gain_stage, throttle, sink)
    return tb


def parse_args():
    parser = argparse.ArgumentParser(description="Transmit a preamble + QAM payload over HackRF")
    parser.add_argument("--modulation", choices=["bpsk", "qpsk", "16qam", "64qam"], default="qpsk")
    parser.add_argument("--freq", type=float, default=433.92e6, help="Center frequency in Hz")
    parser.add_argument("--sample-rate", type=float, default=4e6, help="Sampling rate in Hz")
    parser.add_argument("--symbol-rate", type=float, default=1e6, help="Symbol rate in Hz")
    parser.add_argument("--gain", type=float, default=20.0, help="TX gain in dB")
    return parser.parse_args()


def main():
    args = parse_args()
    tb = build_top_block(args)
    print(f"Starting transmission with modulation={args.modulation}, f={args.freq/1e6:.3f} MHz")
    tb.run()


if __name__ == "__main__":
    main()
