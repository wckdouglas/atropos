import logging
import sys

def detect(options, parser):
    from .detect import create_detector, summarize_contaminants
    from .util import enumerate_range
    
    k = options.kmer_size or 12
    n_reads = options.max_reads or 10000
    overrep_cutoff = 100
    known_contaminants = None
    include = options.include_contaminants or "all"
    if include != 'unknown':
        known_contaminants = load_known_contaminants(options)
    if known_contaminants and include == 'known':
        logging.getLogger().debug("Detecting contaminants using the known-only algorithm")
        detector_class = KnownContaminantDetector
    elif n_reads <= 50000:
        logging.getLogger().debug("Detecting contaminants using the heuristic algorithm")
        detector_class = HeuristicDetector
    else:
        logging.getLogger().debug("Detecting contaminants using the kmer-based algorithm")
        detector_class = KhmerDetector
    
    reader = create_reader(options, parser, counter_magnitude="K", values_have_size=False)[0]
    try:
        with open_output(options.output) as o:
            print("\nDetecting adapters and other potential contaminant sequences based on "
                  "{}-mers in {} reads".format(options.kmer_size, n_reads), file=o)
            
            if options.paired:
                d1, d2 = (detector_class(k, n_reads, overrep_cutoff, known_contaminants) for i in (1,2))
                read1, read2 = next(reader)
                d1.consume_first(read1)
                d2.consume_first(read2)
                for read1, read2 in enumerate_range(reader, 1, n_reads):
                    d1.consume(read1)
                    d2.consume(read2)
                
                summarize_contaminants(o, d1.filter_and_sort(include), n_reads, "File 1: {}".format(reader.reader1.name))
                summarize_contaminants(o, d2.filter_and_sort(include), n_reads, "File 2: {}".format(reader.reader2.name))
                
            else:
                d = detector_class(k, n_reads, overrep_cutoff, known_contaminants)
                d.consume_all(reader)
                summarize_contaminants(o, d.filter_and_sort(include), n_reads, "File: {}".format(reader.name))
    finally:
        reader.close()

def error(options, parser):
    reader, qualities, has_qual_file = create_reader(options, parser, counter_magnitude="K", values_have_size=False)
    try:
        if not qualities:
            parser.error("Cannot estimate error rate without base qualities")
        
        n_reads = options.max_reads or 10000
        
        with open_output(options.error_report) as o:
            if options.paired:
                e1, e1 = (ErrorEstimator() for i in (1,2))
                for read1, read2 in enumerate_range(reader, 1, n_reads):
                    e1.consume(read1.sequence)
                    e2.consume(read2.sequence)
                
                print("File 1: {}".format(reader.reader1.name))
                print("\n  Error rate: {:.2%}".format(e1.estimate()))
                
                print("File 2: {}".format(reader.reader2.name))
                print("\n  Error rate: {:.2%}".format(e2.estimate()))
                
                print("Overall error rate: {:.2%}".format(
                    (e1.total_qual + e2.total_qual) / (e1.total_len / e2.total_len)))
            
            else:
                for read in enumerate_range(reader, 1, n_reads):
                    e.consume(read.sequence)
                
                print("File: {}".format(reader.name))
                print("\n  Error rate: {:.2%}".format(e.estimate()))
    finally:
        reader.close()

def trim(options, parser):
    import time
    import textwrap
    from .report import print_report
    
    params = create_atropos_params(options, parser, options.default_outfile)
    num_adapters = sum(len(a) for a in params.modifiers.get_adapters())
    
    logger = logging.getLogger()
    logger.info("Trimming %s adapter%s with at most %.1f%% errors in %s mode ...",
        num_adapters, 's' if num_adapters > 1 else '', options.error_rate * 100,
        { False: 'single-end', 'first': 'paired-end legacy', 'both': 'paired-end' }[options.paired])
    if options.paired == 'first' and (len(params.modifiers.get_modifiers(read=2)) > 0 or options.quality_cutoff):
        logger.warning('\n'.join(textwrap.wrap('WARNING: Requested read '
            'modifications are applied only to the first '
            'read since backwards compatibility mode is enabled. '
            'To modify both reads, also use any of the -A/-B/-G/-U options. '
            'Use a dummy adapter sequence when necessary: -A XXX')))
    
    start_wallclock_time = time.time()
    start_cpu_time = time.clock()
    
    if options.threads is None:
        # Run single-threaded version
        import atropos.serial
        rc, summary = atropos.serial.run_serial(*params)
    else:
        # Run multiprocessing version
        import atropos.multicore
        rc, summary = atropos.multicore.run_parallel(*params,
            options.threads, options.process_timeout, options.preserve_order, options.read_queue_size,
            options.result_queue_size, not options.no_writer_process, options.compression)
    
    if rc != 0:
        sys.exit(rc)
    
    stop_wallclock_time = time.time()
    stop_cpu_time = time.clock()
    report = print_report(
        options,
        stop_wallclock_time - start_wallclock_time,
        stop_cpu_time - start_cpu_time,
        summary,
        params.modifiers.get_trimmer_classes())

def create_reader(options, parser, counter_magnitude="M", values_have_size=True):
    from .seqio import UnknownFileType, BatchIterator, open_reader
    
    input1 = input2 = qualfile = None
    interleaved = False
    if options.interleaved_input:
        input1 = options.interleaved_input
        interleaved = True
    else:
        input1 = options.input1
        if options.paired:
            input2 = options.input2
        else:
            qualfile = options.input2
    
    try:
        reader = open_reader(input1, file2=input2, qualfile=qualfile,
            colorspace=options.colorspace, fileformat=options.format,
            interleaved=interleaved)
    except (UnknownFileType, IOError) as e:
        parser.error(e)
    
    qualities = reader.delivers_qualities
    
    # Wrap reader in batch iterator
    batch_size = options.batch_size or 1000
    reader = BatchIterator(reader, batch_size, options.max_reads)
    
    # Wrap iterator in progress bar
    if options.progress:
        from .progress import create_progress_reader
        reader = create_progress_reader(
            reader, options.progress, batch_size, options.max_reads,
            counter_magnitude, values_have_size)
    
    return (reader, qualities, qualfile is not None)

from collections import namedtuple
AtroposParams = namedtuple("AtroposParams", ("reader", "modifiers", "filters", "formatters", "writers"))

def create_atropos_params(options, parser, default_outfile):
    from .adapters import AdapterParser, BACK
    from .modifiers import (
        Modifiers, AdapterCutter, InsertAdapterCutter, UnconditionalCutter,
        NextseqQualityTrimmer, QualityTrimmer, NonDirectionalBisulfiteTrimmer,
        RRBSTrimmer, SwiftBisulfiteTrimmer, MinCutter, NEndTrimmer,
        LengthTagModifier, SuffixRemover, PrefixSuffixAdder, DoubleEncoder,
        ZeroCapper, PrimerTrimmer, MergeOverlapping)
    from .filters import (
        Filters, FilterFactory, TooShortReadFilter, TooLongReadFilter,
        NContentFilter, TrimmedFilter, UntrimmedFilter, NoFilter,
        MergedReadFilter)
    from .seqio import Formatters, RestFormatter, InfoFormatter, WildcardFormatter, Writers
    from .util import RandomMatchProbability
    
    reader, qualities, has_qual_file = create_reader(options, parser)
    
    if options.adapter_max_rmp or options.aligner == 'insert':
        match_probability = RandomMatchProbability()
    
    # Create Adapters
    
    parser_args = dict(
        colorspace=options.colorspace,
        max_error_rate=options.error_rate,
        min_overlap=options.overlap,
        read_wildcards=options.match_read_wildcards,
        adapter_wildcards=options.match_adapter_wildcards,
        indels=options.indels, indel_cost=options.indel_cost
    )
    if options.adapter_max_rmp:
        parser_args['match_probability'] = match_probability
        parser_args['max_rmp'] = options.adapter_max_rmp
    adapter_parser = AdapterParser(**parser_args)

    try:
        adapters1 = adapter_parser.parse_multi(options.adapters, options.anywhere, options.front)
        adapters2 = adapter_parser.parse_multi(options.adapters2, options.anywhere2, options.front2)
    except IOError as e:
        if e.errno == errno.ENOENT:
            parser.error(e)
        raise
    except ValueError as e:
        parser.error(e)
    
    # Create Modifiers
    
    if not adapters1 and not adapters2 and not options.quality_cutoff and \
            options.nextseq_trim is None and \
            options.cut == [] and options.cut2 == [] and \
            options.cut_min == [] and options.cut_min2 == [] and \
            (options.minimum_length is None or options.minimum_length <= 0) and \
            options.maximum_length == sys.maxsize and \
            not has_qual_file and options.max_n == -1 and not options.trim_n:
        parser.error("You need to provide at least one adapter sequence.")
    
    if options.aligner == 'insert':
        if options.adapter_pair and adapters1 and adapters2:
            name1, name2 = options.adapter_pair.split(",")
            adapters1 = [a for a in adapters1 if a.name == name1]
            adapters2 = [a for a in adapters2 if a.name == name2]
        if not adapters1 or len(adapters1) != 1 or adapters1[0].where != BACK or \
                not adapters2 or len(adapters2) != 1 or adapters2[0].where != BACK:
            parser.error("Insert aligner requires a single 3' adapter for each read")
    
    if options.debug:
        for adapter in adapters1 + adapters2:
            adapter.enable_debug()
    
    modifiers = Modifiers(options.paired)
            
    for op in options.op_order:
        if op == 'A' and (adapters1 or adapters2):
            # TODO: generalize this using some kind of factory class
            if options.aligner == 'insert':
                # Use different base probabilities if we're trimming bisulfite data.
                # TODO: this doesn't seem to help things, so commenting it out for now
                #base_probs = dict(p1=0.33, p2=0.67) if options.bisulfite else dict(p1=0.25, p2=0.75)
                modifiers.add_modifier(InsertAdapterCutter,
                    adapter1=adapters1[0], adapter2=adapters2[0], action=options.action,
                    mismatch_action=options.correct_mismatches,
                    max_insert_mismatch_frac=options.insert_match_error_rate,
                    max_adapter_mismatch_frac=options.insert_match_adapter_error_rate,
                    match_probability=match_probability,
                    insert_max_rmp=options.insert_max_rmp)
            else:
                a1_args = a2_args = None
                if adapters1:
                    a1_args = dict(adapters=adapters1, times=options.times, action=options.action)
                if adapters2:
                    a2_args = dict(adapters=adapters2, times=options.times, action=options.action)
                modifiers.add_modifier_pair(AdapterCutter, a1_args, a2_args)
        elif op == 'C' and (options.cut or options.cut2):
            modifiers.add_modifier_pair(UnconditionalCutter,
                dict(lengths=options.cut),
                dict(lengths=options.cut2)
            )
        elif op == 'G' and (options.nextseq_trim is not None):
            modifiers.add_modifier(NextseqQualityTrimmer,
                read=1, cutoff=options.nextseq_trim, base=options.quality_base)
        elif op == 'Q' and options.quality_cutoff:
            modifiers.add_modifier(QualityTrimmer,
                cutoff_front=options.quality_cutoff[0],
                cutoff_back=options.quality_cutoff[1],
                base=options.quality_base)
    
    if options.bisulfite:
        if isinstance(options.bisulfite, str):
            if "non-directional" in options.bisulfite:
                modifiers.add_modifier(NonDirectionalBisulfiteTrimmer,
                    rrbs=options.bisulfite=="non-directional-rrbs")
            elif options.bisulfite == "rrbs":
                modifiers.add_modifier(RRBSTrimmer)
            elif options.bisulfite in ("epignome", "truseq"):
                # Trimming leads to worse results
                #modifiers.add_modifier(TruSeqBisulfiteTrimmer)
                pass
            elif options.bisulfite == "swift":
                modifiers.add_modifier(SwiftBisulfiteTrimmer)
        else:
            if options.bisulfite[0]:
                modifiers.add_modifier(MinCutter, read=1, **(options.bisulfite[0]))
            if len(options.bisulfite) > 1 and options.bisulfite[1]:
                modifiers.add_modifier(MinCutter, read=2, **(options.bisulfite[1]))
    
    if options.trim_n:
        modifiers.add_modifier(NEndTrimmer)
    
    if options.cut_min or options.cut_min2:
        modifiers.add_modifier_pair(MinCutter,
            dict(lengths=options.cut_min),
            dict(lengths=options.cut_min2)
        )
    
    if options.length_tag:
        modifiers.add_modifier(LengthTagModifier, length_tag=options.length_tag)
    
    if options.strip_suffix:
        modifiers.add_modifier(SuffixRemover, suffixes=options.strip_suffix)
    
    if options.prefix or options.suffix:
        modifiers.add_modifier(PrefixSuffixAdder, prefix=options.prefix, suffix=options.suffix)
    
    if options.double_encode:
        modifiers.add_modifier(DoubleEncoder)
    
    if options.zero_cap and qualities:
        modifiers.add_modifier(ZeroCapper, quality_base=options.quality_base)
    
    if options.trim_primer:
        modifiers.add_modifier(PrimerTrimmer)
    
    if options.merge_overlapping:
        modifiers.add_modifier(MergeOverlapping,
            min_overlap=options.merge_min_overlap,
            error_rate=options.error_rate)
    
    # Create Filters and Formatters
    
    min_affected = 2 if options.pair_filter == 'both' else 1
    filters = Filters(FilterFactory(options.paired, min_affected))
    
    output1 = output2 = None
    interleaved = False
    if options.interleaved_output:
        output1 = options.interleaved_output
        interleaved = True
    else:
        output1 = options.output
        output2 = options.paired_output
    
    seq_formatter_args = dict(
        qualities=qualities,
        colorspace=options.colorspace,
        interleaved=interleaved
    )
    formatters = Formatters(output1, seq_formatter_args)
    force_create = []
        
    if (options.merge_overlapping and options.merged_output):
        formatters.add_seq_formatter(MergedReadFilter, options.merged_output)
        
    if options.minimum_length is not None and options.minimum_length > 0:
        filters.add_filter(TooShortReadFilter, options.minimum_length)
        if options.too_short_output:
            formatters.add_seq_formatter(TooShortReadFilter,
                options.too_short_output, options.too_short_paired_output)

    if options.maximum_length < sys.maxsize:
        filters.add_filter(TooLongReadFilter, options.maximum_length)
        if options.too_long_output is not None:
            formatters.add_seq_formatter(TooLongReadFilter,
                options.too_long_output, options.too_long_paired_output)

    if options.max_n >= 0:
        filters.add_filter(NContentFilter, options.max_n)

    if options.discard_trimmed:
        filters.add_filter(TrimmedFilter)

    if not formatters.multiplexed:
        if output1 is not None:
            formatters.add_seq_formatter(NoFilter, output1, output2)
            if output1 != "-" and not options.no_writer_process:
                force_create.append(output1)
                if output2 is not None:
                    force_create.append(output2)
        elif not (options.discard_trimmed and options.untrimmed_output):
            formatters.add_seq_formatter(NoFilter, default_outfile)
            if default_outfile != "-" and not options.no_writer_process:
                force_create.append(default_outfile)
    
    if options.discard_untrimmed or options.untrimmed_output:
        filters.add_filter(UntrimmedFilter)

    if not options.discard_untrimmed:
        if formatters.multiplexed:
            untrimmed = options.untrimmed_output or output1.format(name='unknown')
            formatters.add_seq_formatter(UntrimmedFilter, untrimmed)
            formatters.add_seq_formatter(NoFilter, untrimmed)
        elif options.untrimmed_output:
            formatters.add_seq_formatter(UntrimmedFilter,
                options.untrimmed_output, options.untrimmed_paired_output)

    if options.rest_file:
        formatters.add_info_formatter(RestFormatter(options.rest_file))
    if options.info_file:
        formatters.add_info_formatter(InfoFormatter(options.info_file))
    if options.wildcard_file:
        formatters.add_info_formatter(WildcardFormatter(options.wildcard_file))
    
    writers = Writers(force_create)
    
    return AtroposParams(reader, modifiers, filters, formatters, writers)