#!/usr/bin/env python
# Summarize the mapping results of trimmed versus untrimmed reads.
# This requires that both input bams be name-sorted.

from collections import defaultdict
from common import *
import csv
import pylev

nuc = ('A','C','G','T','N')

class Hist(object):
    def __init__(self, n_bases):
        # position base histograms
        self.hists = [
            [
                dict((base, [0] * n_bases) for base in ('A','C','G','T','N'))
                for i in range(2)
            ]
            for j in range(2)
        ]
    
    def add_reads(self, r1, r2):
        # add start and end bases to histogram
        for seq, hist in zip((r1.sequence, r2.sequence), (self.hists)):
            for i, b in enuermate_range(seq, 0, n_bases):
                hist[b][i] += 1
            for i, b in enuermate_range(reversed(seq), 0, n_bases):
                hist[b][i] += 1

pair_fields = [
    ('prog', None),
    ('read_name', None),
    ('read_idx', None),
    ('discarded', False),
    ('split', False),
    ('skipped', False),
    ('proper', False)
]
read_fields = [
    ('mapped', False),
    ('quality', None),
    ('chrm', None),
    ('start', None),
    ('end', None),
    ('trimmed_front', None),
    ('trimmed_back', None),
    ('clipped_front', None),
    ('clipped_back', None)
]

def write_header(w):
    rf = [key for key, val in read_fields]
    r1 = ['read1_{}'.format(key) for key in rf]
    r2 = ['read2_{}'.format(key) for key in rf]
    w.writerow([key for key, val in pair_fields] + r1 + r2)

class TableRead(object):
    def __init__(self):
        for key, val in read_fields:
            setattr(self, key, val)
    
    def set_from_read(self, r):
        self.mapped = not r.is_unmapped
        self.quality = r.mapping_quality
        self.chrm = r.reference_name
        self.start = r.reference_start
        self.end = r.reference_end
        
        if self.mapped:
            def count_soft_clipped(seq):
                clipped = 0
                for pos in seq:
                    if pos:
                        break
                    else:
                        clipped += 1
                return clipped

            ref_pos = r.get_reference_positions(full_length=True)
            self.clipped_front = count_soft_clipped(ref_pos)
            self.clipped_back = count_soft_clipped(reversed(ref_pos))
    
    def set_trimming(self, u, t):
        untrimmed = u.query_sequence.upper()
        untrimmed_len = len(untrimmed)
        trimmed = t.query_sequence.upper()
        trimmed_len = len(trimmed)
        
        trimmed_front = 0
        if untrimmed_len > trimmed_len:
            for i in range(untrimmed_len - trimmed_len):
                if untrimmed[i:(i+trimmed_len)] == trimmed:
                    trimmed_front = i
                    break
            else:
                # Since Skewer performs automatic error correction, the trimmed and
                # untrimmed reads may not match, so in that case we find the closest
                # match by Levenshtein distance.
                dist = None
                for i in range(untrimmed_len - trimmed_len):
                    d = pylev.levenshtein(untrimmed[i:(i+trimmed_len)], trimmed)
                    if not dist:
                        dist = d
                    elif d < dist:
                        trimmed_front = i
                        dist = d
        
        self.trimmed_front = trimmed_front
        self.trimmed_back = untrimmed_len - (trimmed_len + trimmed_front)

class TableRow(object):
    def __init__(self, prog, read_name, read_idx):
        for key, val in pair_fields:
            setattr(self, key, val)
        self.prog = prog
        self.read_name = read_name
        self.read_idx = read_idx
        self.read1 = TableRead()
        self.read2 = TableRead()
    
    def set_from_pair(self, r1, r2):
        assert r1.is_proper_pair == r2.is_proper_pair
        self.proper = r1.is_proper_pair
        self.read1.set_from_read(r1)
        self.read2.set_from_read(r2)
    
    def write(self, w):
        w.writerow([
            getattr(self, key) for key, val in pair_fields
        ] + [
            getattr(self.read1, key) for key, val in read_fields
        ] + [
            getattr(self.read2, key) for key, val in read_fields
        ])

def summarize(untrimmed, trimmed, ref, ow, hw, n_hist_bases=20):
    progs = ('untrimmed',) + tuple(trimmed.keys())
    hists = dict((prog, hist) for prog in progs)
    
    for i, (name, u1, u2) in tqdm.tqdm(enumerate(untrimmed), 1):
        assert len(u1) > 0 and len(u2) > 0
        
        rows = dict(untrimmed=TableRow('untrimmed', name, i))
        valid = {}
        
        for prog, bam in trimmed.items():
            rows[prog] = TableRow(prog, name, i)
            if bam.peek[0] == name:
                _, t1, t2 = next(bam)
                assert len(t1) > 0 and len(t2) > 0
                if len(t1) > 1 or len(t2) > 1:
                    rows[prog].split = True
                else:
                    t1 = t1[0]
                    t2 = t2[0]
                    hists[prog].add_reads(t1, t2)
                    valid[prog] = (t1, t2)
            else:
                rows[prog].discarded = True
        
        if len(u1) > 1 or len(u2) > 1:
            rows['untrimmed'].split = True
            for row in rows.values():
                row.skipped = True
        else:
            u1 = u1[0]
            u2 = u2[0]
            hists['untrimmed'].add_reads(u1, u2)
            rows['untrimmed'].set_from_pair(u1, u2)
            if len(valid) == 0:
                rows['untrimmed'].skip = True
            else:
                for prog, (r1, r2) in valid.items():
                    row = rows[prog]
                    row.set_from_pair(r1, r2)
                    row.read1.set_trimming(u1, r1)
                    row.read2.set_trimming(u2, r2)
            
        for prog in progs:
            rows[prog].write(ow)
    
    for i, h in enumerate(hists, 1):
        for j in range(2):
            for b in nuc:
                for k, count in enumerate(h[j][b], 1):
                    hw.writerow((i, j, k, b, count))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--trimmed-bam", nargs="+",
        help="One or more name=file arguments to identify trimmed BAM files. These BAMs "
             "must be name-sorted.")
    parser.add_argument("-u", "--untrimmed-bam")
    parser.add_argument("-o", "--output", default="-")
    parser.add_argument("-h", "--hist", default="trimmed_hists.txt")
    args = parser.parse_args()
    
    untrimmed = BAMReader(args.untrimmed_bam)
    trimmed = {}
    for arg in args.trimmed_bam:
        name, path = arg.split('=')
        trimmed[name] = BAMReader(path)
    
    try:
        with open_output(args.output) as o, open_output(args.hist) as h:
            ow = csv.writer(o, delimiter="\t")
            write_header(ow)
            hw = csv.writer(h, delimiter="\t")
            hw.writerow(('read', 'side', 'pos', 'base', 'count'))
            summarize(untrimmed, trimmed, ow, hw)
    finally:
        untrimmed.close()
        for t in trimmed.values():
            t.close()

if __name__ == "__main__":
    main()

# This is code to compare the mapped reads against the reference.
# from pysam import FastaFile
#parser.add_argument("-r", "--reference",
#    help="Reference sequence. If reads are bisulfite converted, then this must be a "
#         "bisulfite-converted reference.")
#parser.add_argument("-a", "--adapter1", default=ADAPTER1)
#parser.add_argument("-A", "--adapter2", default=ADAPTER2)
#parser.add_argument("-q", "--mapqs", nargs='*', default=(0,10,20,30,40))
# with FastaFile(args.reference) as ref,
# chrm = t.reference_name
# if bisulfite:
#     chrm = ('f','r')[int(t.is_rever)] + chrm
# chrm_len = t.get_reference_length(chrm)
# ref_start = t.reference_start - offset_front
# ref_end = t.reference_end + offset_back
# if ref_start < 0:
#     untrimmed = untrimmed[abs(ref_start):]
#     offset_front += ref_start
#     ref_start = 0
# if ref_end > chrm_len:
#     overhang = chrm_len - ref_end
#     untrimmed = untrimmed[:overhang]
#     offset_back += overhang
#     ref_end = chrm_len
#
# ref_seq = ref.fetch(chrm, ref_start, ref_end)
# assert len(ref_seq) == len(untrimmed)
#
# if bisulfite:
#     untrimmed = bisulfite_convert(untrimmed, r)
#     trimmed = bisulfite_convert(trimmed, r)
#
# def compare_seqs(s1, s2):
#     n = 0
#     for i, (b1, b2) in enumerate(zip(s1, s2)):
#         if b1 == 'N' or b2 == 'N':
#             n += 1
#         elif b1 == b2:
#             n = 0
#         else:
#             break
#     return i - n
#
# if undertrimmed_front == 0 and offset_start > 0:
#     overtrimmed_front = compare_seqs(untrimmed[offset_front:0:-1], ref_seq[offset_front:0:-1])
#
# if undertrimmed_back == 0 and offset_back > 0:
#     overtrimmed_back = compare_seqs(untrimmed[offset_back:], ref_seq[offset_back:]))
#
# def bisulfite_convert(seq, read):
#     if read == 0:
#         return seq.replace('C', 'T')
#     else:
#         return seq.replace('G', 'A')
#
# ADAPTER1="GATCGGAAGAGCACACGTCTGAACTCCAGTCACCAGATCATCTCGTATGCCGTCTTCTGCTTG" # TruSeq index 7
# ADAPTER2="AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGTAGATCTCGGTGGTCGCCGTATCATT" # TruSeq universal