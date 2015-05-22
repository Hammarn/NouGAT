import sys, os, yaml, glob
import subprocess
import pandas as pd
import gzip
import re
import string
import shutil
from matplotlib import pyplot as plt
from nougat import common, align, pdf
from nougat.pdf.theme import colors, DefaultTheme


def run(global_config, sample_config):
    sorted_libraries_by_insert = common._sort_libraries_by_insert(
            sample_config)
    if "tools" in sample_config:
        """If so, execute them one after the other in the specified order
        (might not work)"""
        for command in sample_config["tools"]:
            """with this I pick up at run time the correct function in the
            current module"""
            command_fn = getattr(sys.modules[__name__],
                    "_run_{}".format(command))
            """Update sample config, each command return sample_config and if
            necessary it modifies it"""
            sample_config = command_fn(global_config, sample_config,
                    sorted_libraries_by_insert)
    else:
        #run default pipeline for QC
        sample_config = _run_fastqc(global_config, sample_config,
                sorted_libraries_by_insert)
        sample_config = _run_abyss(global_config, sample_config,
                sorted_libraries_by_insert)
        sample_config = _run_trimmomatic(global_config, sample_config,
                sorted_libraries_by_insert)
    _run_report(global_config, sample_config,sorted_libraries_by_insert)


def _run_align(global_config, sample_config,sorted_libraries_by_insert):

    if "reference" not in sample_config:
        print "reference sequence not provided, skypping alignment step. "
        "Please provide a reference if you are intrested in aligning the reads "
        "against a reference"
        return sample_config
    if not os.path.exists("alignments"):
        os.makedirs("alignments")
    os.chdir("alignments")

    sorted_libraries_by_insert =  align._align_reads(global_config,
            sample_config,  sorted_libraries_by_insert) # align reads
    sorted_alignments_by_insert = align._merge_bam_files(global_config,
            sample_config, sorted_libraries_by_insert) # merge alignments
    sorted_alignments_by_insert = align.picard_CGbias(global_config,
            sample_config,sorted_alignments_by_insert) # compute picard stats
    sorted_alignments_by_insert = align.picard_collectInsertSizeMetrics(
            global_config, sample_config,sorted_alignments_by_insert)
    sorted_alignments_by_insert = align.picard_markDuplicates(global_config,
            sample_config,sorted_alignments_by_insert)

    os.chdir("..")
    sample_config["alignments"] = sorted_alignments_by_insert

    return sample_config


def _run_fastqc(global_config, sample_config, sorted_libraries_by_insert):
    mainDir = os.getcwd()
    FastqcFolder = os.path.join(os.getcwd(), "fastqc")
    if not os.path.exists(FastqcFolder):
        os.makedirs(FastqcFolder)

    program=global_config["Tools"]["fastqc"]["bin"]
    program_options=global_config["Tools"]["fastqc"]["options"]
    for library, libraryInfo in sorted_libraries_by_insert:
        command = [program]
        for option in program_options:
            command.append(option)
        read1=libraryInfo["pair1"]
        read2=libraryInfo["pair2"]
        command.append(read1)
        if read2 is not None:
            command.append(read2)
        common.print_command(command)
        folder_output_name = os.path.join(FastqcFolder,
                os.path.basename(read1).split(".fastq.gz")[0])
        if not common.check_dryrun(sample_config) and not \
                os.path.exists("{}_fastqc.zip".format(folder_output_name)):
            fastq_stdOut = open(os.path.join(FastqcFolder,
                    "{}_fastqc.stdout".format(library)), "a")
            fastq_stdErr = open(os.path.join(FastqcFolder,
                    "{}_fastqc.stderr".format(library)), "a")
            subprocess.call(command, stdout=fastq_stdOut, stderr=fastq_stdErr)
    sample_config["fastqc"] = FastqcFolder
    return sample_config


def _run_abyss(global_config, sample_config, sorted_libraries_by_insert):
    mainDir = os.getcwd()
    ABySS_Kmer_Folder = os.path.join(os.getcwd(), "abyss_kmer")
    if "kmer" not in sample_config:
        sys.exit("error in _run_abyss QCcontrol: kmer must be present in \
                sample_config.yaml")

    kmer = sample_config["kmer"]
    if not os.path.exists(ABySS_Kmer_Folder):
        os.makedirs(ABySS_Kmer_Folder)

    os.chdir(ABySS_Kmer_Folder)

    program = global_config["Tools"]["abyss"]["bin"]
    program = os.path.join(os.path.dirname(program), "ABYSS-P")
    program_options=global_config["Tools"]["abyss"]["options"]
    if "abyss" in sample_config:
        program_options=sample_config["abyss"]

    threads = 16 # default for UPPMAX
    if "threads" in sample_config :
        threads = sample_config["threads"]

    command = "mpirun -np {} {} ".format(threads, program)
    command += "-k {} ".format(kmer)
    command += "--coverage-hist=histogram.hist -o preUnitgs.fa"
    for library, libraryInfo in sorted_libraries_by_insert:
        read1=libraryInfo["pair1"]
        read2=libraryInfo["pair2"]
        orientation = libraryInfo["orientation"]
        if orientation=="innie" or orientation=="outtie":
            command += " {} ".format(read1)
            if read2 is not None:
                command += " {} ".format(read2)
        if orientation == "none":
            command += " {} ".format(read1)

    common.print_command(command)
    if not common.check_dryrun(sample_config) and not \
            os.path.exists("histogram.hist"):
        ABySS_Kmer_stdOut = open("ABySS_Kmer_Folder.stdOut", "a")
        ABySS_Kmer_stdErr = open("ABySS_Kmer_Folder.stdErr", "a")
        returnValue = subprocess.call(command, shell=True, \
                stdout=ABySS_Kmer_stdOut, stderr=ABySS_Kmer_stdErr)
        if returnValue > 0:
            print "ABySS kmer plotting failed: unkwnown reason"
        else :
            subprocess.call(("rm", "preUnitgs.fa"))
            _plotKmerFixed(1,200, kmer, "kmer_coverage_1_200.png")
            _plotKmerFixed(1,500, kmer, "kmer_coverage_1_500.png")
            _plotKmerFixed(15,200, kmer, "kmer_coverage_15_200.png")
            _plotKmerFixed(15,500, kmer, "kmer_coverage_15_500.png")
            _plotKmer(kmer, "kmer_coverage.png")

    os.chdir("..")
    sample_config["abyss"] = ABySS_Kmer_Folder
    return sample_config


def _plotKmer(kmer, output_name):
    """Kmer abundance as a single plot, suitable for the report
    """
    Kmer_histogram = pd.io.parsers.read_csv("histogram.hist", sep='\t',
            header=None)
    Kmer_coverage = Kmer_histogram[Kmer_histogram.columns[0]].tolist()
    Kmer_count = Kmer_histogram[Kmer_histogram.columns[1]].tolist()

    # Not interested in coverage > 5000
    kcov = [c for c in Kmer_coverage if c <= 5000]
    # Multiply the abundance by a gradient
    kcount_gradient = [kcov[i] * Kmer_count[i] for i in range(len(kcov))]

    first_valley = kcount_gradient.index(min(kcount_gradient[0:100]))
    after_valley = kcount_gradient[first_valley:]
    avg_count = sum(after_valley) / len(after_valley)

    # This hack seems to work reasonably well. Set the the xlimit for 
    # the plot to be the average kmer abundance in the interval fm 
    # first valley to 5000.
    xmax = min(range(len(after_valley)),
            key=lambda i: abs(after_valley[i]-avg_count))
    xmax += first_valley

    # It might happen
    if xmax < 200:
        xmax = 200

    ymax = max(kcount_gradient)
    peak = kcount_gradient.index(max(after_valley))
    plt.xlim((0, xmax))
    plt.ylim((0, ymax))
    plt.plot(kcov, kcount_gradient)

    plt.vlines(peak, 1, kcount_gradient[peak], colors='r',
        linestyles='--')
    plt.text(peak, kcount_gradient[peak], str(peak))

    plotname = "{}".format(output_name)
    plt.savefig(plotname)
    plt.clf()
    return 0


def _plotKmerFixed(min_limit, max_limit, kmer, output_name):
    """Old kmerplot, kept just in case...
    """
    Kmer_histogram = pd.io.parsers.read_csv("histogram.hist", sep='\t',
            header=None)
    Kmer_coverage = Kmer_histogram[Kmer_histogram.columns[0]].tolist()
    Kmer_count = Kmer_histogram[Kmer_histogram.columns[1]].tolist()
    Kmer_freq = [Kmer_coverage[i]*Kmer_count[i] for i in \
            range(len(Kmer_coverage))]
    #coverage peak, disregarding initial peak
    kmer_freq_peak = Kmer_freq.index(max(Kmer_freq[min_limit:max_limit]))
    kmer_freq_peak_value=max(Kmer_freq[min_limit:max_limit])

    xmax = max_limit
    ymax = kmer_freq_peak_value + (kmer_freq_peak_value*0.30)

    plt.plot(Kmer_coverage, Kmer_freq)
    plt.title("K-mer length = {}".format(kmer))
    plt.xlim((0,xmax))
    plt.ylim((0,ymax))
    plt.vlines(kmer_freq_peak, 0, kmer_freq_peak_value, colors='r',
            linestyles='--')
    plt.text(kmer_freq_peak, kmer_freq_peak_value+2000, str(kmer_freq_peak))
    plotname = "{}".format(output_name)
    plt.savefig(plotname)
    plt.clf()
    return 0


def _run_trimmomatic(global_config, sample_config, sorted_libraries_by_insert):
    program        = global_config["Tools"]["trimmomatic"]["bin"]
    program_folder = os.path.dirname(program)
    if "adapters" not in sample_config:
        sys.exit("running MP pipeline, adapters file to be used in trimming"
                "are needed for Trimmomatic. Please specify them"
                "in the sample configuration file and rerun")
    adapterFile    = sample_config["adapters"]
    if not os.path.exists(adapterFile):
        sys.exit("Trimmomatic cannot be run as adapter file is not specified"
                "or points to unknown position: {}".format(adapterFile))

    mainDirectory = os.getcwd()
    trimmomaticDir = os.path.join(mainDirectory, "Trimmomatic")
    if not os.path.exists(trimmomaticDir):
        os.makedirs(trimmomaticDir)
    os.chdir(trimmomaticDir)
    #now I am in running dir, I need to process one by one the libraries
    threads = 8
    if "threads" in sample_config:
        threads = sample_config["threads"]

    for library, libraryInfo in sorted_libraries_by_insert:
        read1=libraryInfo["pair1"]
        read2=libraryInfo["pair2"]
        orientation = libraryInfo["orientation"]
        if read2 is not None:
            read1_baseName = os.path.split(read1)[1].split(".")[0]
            read2_baseName = os.path.split(read2)[1].split(".")[0]
            output_read1_pair = os.path.join(trimmomaticDir,
                    "{}.fastq.gz".format(read1_baseName))
            output_read1_sing = os.path.join(trimmomaticDir,
                    "{}_u.fastq.gz".format(read1_baseName))
            output_read2_pair = os.path.join(trimmomaticDir,
                    "{}.fastq.gz".format(read2_baseName))
            output_read2_sing = os.path.join(trimmomaticDir,
                    "{}_u.fastq.gz".format(read2_baseName))
            command = ["java",  "-jar", program, "PE", "-threads",
                    "{}".format(threads),  "-phred33",  read1, read2,
                    output_read1_pair, output_read1_sing, output_read2_pair,
                    output_read2_sing,
                    "ILLUMINACLIP:{}:2:30:10".format(adapterFile),
                    "LEADING:3", "TRAILING:3", "SLIDINGWINDOW:4:15",
                    "MINLEN:30"]
            common.print_command(command)
            # do not execute is files have been already gennerated
            if not common.check_dryrun(sample_config) and not \
                    os.path.exists(output_read1_pair):
                stdOut = open("{}_trimmomatic.stdOut".format(read1_baseName),
                        "w")
                stdErr = open("{}_trimmomatic.stdErr".format(read1_baseName),
                        "w")
                returnValue = subprocess.call(command, stdout=stdOut,
                        stderr=stdErr) # run the program
                if returnValue != 0:
                    print "error while running command: {}".format(command)
            libraryInfo["pair1"] = output_read1_pair
            libraryInfo["pair2"] = output_read2_pair
            libraryInfo["trimmomatic"] = os.path.join(trimmomaticDir,
                    "{}_trimmomatic.stdErr".format(read1_baseName))
    os.chdir(mainDirectory)
    return sample_config


def _kmergenie_plot(hist_file):
    """Kmergenie outputs pdf plots. We want pngs without resorting to \
    imagemagick
    TODO: Abstract this to a common plotting function if possible"""
    kgenie_hist = pd.io.parsers.read_csv(hist_file, sep=" ", header=0)
    kmer_lengths = kgenie_hist[kgenie_hist.columns[0]].tolist()
    genomic_kmers = kgenie_hist[kgenie_hist.columns[1]].tolist()
    peak_value = max(genomic_kmers)
    peak_idx = genomic_kmers.index(peak_value)
    best_k = kmer_lengths[peak_idx]

    plt.plot(kmer_lengths, genomic_kmers)
    plt.title("Best K-mer length: {}".format(best_k))
    plt.xlabel("K-mer size")
    plt.ylabel("Number of genomic k-mers")
    y_margin = (min(genomic_kmers) + peak_value) / 2 * 0.01
    y_min = min(genomic_kmers) - y_margin
    y_max = peak_value + y_margin
    plt.ylim(y_min, y_max)
    plt.xlim(min(kmer_lengths) - 5, max(kmer_lengths) + 5)
    plt.vlines(best_k, 0, peak_value, colors = "r", linestyles='--')

    plt.savefig(hist_file + ".png")
    plt.clf()


def _run_kmergenie(global_config, sample_config, sorted_libraries_by_insert):
    """Runs kmergenie to establish a recommended kmer size for assembly"""

    maindir = os.getcwd()
    kmerdir = os.path.join(maindir, "kmergenie")
    if not os.path.exists(kmerdir):
        os.makedirs(kmerdir)
    os.chdir(kmerdir)

    #Write a list of input fastq files for kmergenie
    kmer_input = os.path.join(kmerdir,
            "{}kmerinput.txt".format(sample_config.get("output","")))

    program = global_config["Tools"]["kmergenie"]["bin"]
    program_options=global_config["Tools"]["kmergenie"]["options"]
    # Could be useful to add --diploid if sample is highly heterozygous
    if "kmergenie" in sample_config:
        program_options=sample_config["kmergenie"]

    threads = "" # Kmergenie will spawn number_of_cores - 1 threads by default
    if "threads" in sample_config :
        threads = sample_config["threads"]

    cmd_list = [program, kmer_input]
    for option in filter(None, program_options):
        cmd_list.append(option)
    if threads:
        cmd_list.append("-t {}".format(threads))
    command = " ".join(cmd_list)
    common.print_command(command)

    if not common.check_dryrun(sample_config):
        with open(kmer_input, "w") as f:
            for lib, lib_info in sorted_libraries_by_insert:
                f.write(lib_info["pair1"] + "\n")
                f.write(lib_info["pair2"] + "\n")

        stdOut = open("kmergenie.stdOut", "w")
        stdErr = open("kmergenie.stdErr", "w")
        returnValue = subprocess.call(cmd_list, stdout=stdOut, stderr=stdErr)
        if returnValue != 0:
            print "error while running command: {}".format(command)
        else:
            _kmergenie_plot("histograms.dat")
    sample_config["kmergenie"] = kmerdir
    os.chdir(maindir)
    return sample_config

def _run_report(global_config, sample_config, sorted_libraries_by_insert):
    """This function produces a pdf report and stores the important \
            resutls in a single folder"""

    ### retrive all info needed to write the report
    sampleName = "sample"
    if "output" in sample_config:
        sampleName = sample_config["output"]
    projectName = "anonymous_project"
    if "projectName" in sample_config:
        projectName = sample_config["projectName"]

    currentDir  = os.getcwd()
    workingDir  = os.path.join(currentDir, "results")
    if not os.path.exists(workingDir):
        os.makedirs(workingDir)
    os.chdir(workingDir)

    reportDir   = os.path.join(workingDir, "report")
    if not os.path.exists(reportDir):
        os.makedirs(reportDir)

    PDFtitle = os.path.join(workingDir, "report",
            "{}.pdf".format(sample_config["output"]))

    # this you cannot do in rLab which is why I wrote the helper initially
    TABLE_WIDTH = 540
    class MyTheme(DefaultTheme):
        doc = {
            'leftMargin': 25,
            'rightMargin': 25,
            'topMargin': 20,
            'bottomMargin': 25,
            'allowSplitting': False
            }
    # let's create the doc and specify title and author
    doc = pdf.Pdf('{} {}'.format(projectName, sampleName),
            'NGI-Stockholm, Science for Life Laboratory')

    # now we apply our theme
    doc.set_theme(MyTheme)
    # give me some space
    doc.add_spacer()
    # this header defaults to H1
    scriptDirectory = os.path.split(os.path.abspath(__file__))[0]
    logo_path = os.path.join(scriptDirectory, '../pictures/ngi_scilife.png')
    doc.add_image(logo_path, 540, 50, pdf.CENTER)
    # give me some space
    doc.add_spacer()

    doc.add_header('NGI-Stockholm -- Science For Life Laboratory')
    doc.add_header('Best-practice analysis for quality checking report')
    doc.add_header('{} -- {}'.format(projectName, sampleName))
    # give me some space
    doc.add_spacer()
    doc.add_paragraph("For sample {} belonging to the project {} "
            "NGI-Stockholm best-practice analysis for quality checking has "
            "been performed. For mate pair libraries produced with Nextera, "
            "best-practice analysis described at this address has been "
            "performed: http://res.illumina.com/documents/products/technotes/"
            "technote_nextera_matepair_data_processing.pdf".format(sampleName,
            projectName))
    doc.add_spacer()
    tools = ["trimmomatic", "fastqc", "abyss", "align", "kmergenie"]
    if "tools" in sample_config and len(sample_config["tools"]) > 0:
        tools = sample_config["tools"]
    doc.add_paragraph("The following tools have been employed \
            (tools are listed in order of execution):")
    bollet_list = []
    for tool in tools:
        if tool != "align":
            program_path = global_config["Tools"][tool]["bin"]
            bollet_list.append("{} : {}".format(tool, program_path))
        else:
            bollet_list.append("{} : {}".format(tool, \
                    global_config["Tools"]["bwa"]["bin"]))
            bollet_list.append("{} : {}".format(tool, \
                    global_config["Tools"]["samtools"]["bin"]))
            bollet_list.append("{} : {}".format(tool, \
                    global_config["Tools"]["picard"]["bin"]))
    doc.add_list(bollet_list)
    doc.add_spacer()
    doc.add_paragraph("The results from each tool is reported in the "
            "following sections. Moreover you will find all the results and "
            "commands that have been run in the delivery folder on Uppmax")

    for tool in tools:
	doc.add_pagebreak()
        doc.add_header(tool.title() , pdf.H2)
        if tool  == "trimmomatic":
            doc.add_paragraph("Reads (both paired and mate pairs) can "
                    "contain parts of the adapter sequence or, in the case of "
                    "mate pairs, part of the linker sequence. Illumina "
                    "recommends to remove the adapter before use of the reads "
                    "in any downstream analysis (this is mandatory for mate "
                    "pairs).")
            doc.add_paragraph("Adapter sequences removed are:")
            adapter_file = sample_config["adapters"]
            adapters     = []
            with open(adapter_file) as file:
                lines       = file.readlines()
                for index in xrange(1, len(lines), 2):
                    adapters.append(lines[index].rstrip())
            doc.add_list(adapters)
            doc.add_spacer()

            trimmomatic_table_part1 = [[sampleName, "#orig_pairs",
                    "#survived_pairs"]] # this is the header row
            trimmomatic_table_part2 = [[sampleName,"#survived_fw_only",
                    "#survived_rv_only", "#discarded"]]

            total_orig_pairs = 0
            total_survived_pairs = 0
            total_survived_fw_only = 0
            total_survived_rv_only = 0
            total_discarded = 0

            for library, libraryInfo in sorted_libraries_by_insert:
                runName = os.path.basename(libraryInfo["trimmomatic"]).split(
                        "_1_trimmomatic.stdErr")[0]
                with open(libraryInfo["trimmomatic"]) as trimmomatic_output:
                    lines       = trimmomatic_output.readlines()
                    result_line = lines[-2].rstrip()
                    match_string = re.compile("Input Read Pairs: (\d+) Both "
                            "Surviving: (\d+) \(.+\) Forward Only Surviving: "
                            "(\d+) \(.+\) Reverse Only Surviving: (\d+) \(.+\) "
                            "Dropped: (\d+) \(.+\)")
                    read_pairs = int(match_string.match(result_line).group(1))
                    survived_pairs = int(match_string.match(
                        result_line).group(2))
                    survived_fw_only = int(match_string.match(
                        result_line).group(3))
                    survived_rv_only = int(match_string.match(
                        result_line).group(4))
                    discarded        = int(match_string.match(
                        result_line).group(5))
                    read_pairs_perc =  "({0:.0f}%)".format(
                            (float(survived_pairs)/read_pairs) * 100)
                    survived_fw_only_perc = "({0:.0f}%)".format(
                            (float(survived_fw_only)/read_pairs) * 100)
                    survived_rv_only_perc = "({0:.0f}%)".format(
                            (float(survived_rv_only)/read_pairs) * 100)
                    survived_discarded_perc = "({0:.0f}%)".format(
                            (float(discarded)/read_pairs) * 100)

                    total_orig_pairs += read_pairs
                    total_survived_pairs += survived_pairs
                    total_survived_fw_only += survived_fw_only
                    total_survived_rv_only += survived_rv_only
                    total_discarded += discarded
                # these are the other rows
                trimmomatic_table_part1.append([runName,read_pairs,
                    "{} {}".format(survived_pairs, read_pairs_perc)])
                trimmomatic_table_part2.append([runName,
                    "{} {}".format(survived_fw_only, survived_fw_only_perc),
                    "{} {}".format(survived_rv_only, survived_rv_only_perc),
                    "{} {}".format(discarded, survived_discarded_perc)])
            survived_pairs_perc =  "({0:.0f}%)".format(
                    (float(total_survived_pairs)/total_orig_pairs) * 100)
            survived_survived_fw_only_perc =  "({0:.0f}%)".format(
                    (float(total_survived_fw_only)/total_orig_pairs) * 100)
            survived_survived_rv_only_perc =  "({0:.0f}%)".format(
                    (float(total_survived_rv_only)/total_orig_pairs) * 100)
            survived_discarded_perc =  "({0:.0f}%)".format(
                    (float(total_discarded)/total_orig_pairs) * 100)
            trimmomatic_table_part1.append(["total", total_orig_pairs,
                "{} {}".format(total_survived_pairs, survived_pairs_perc)])
            # last row is the sum
            trimmomatic_table_part2.append(["total", "{} {}".format(
                survived_fw_only, survived_fw_only_perc),
                "{} {}".format(survived_rv_only, survived_rv_only_perc),
                "{} {}".format(discarded, survived_discarded_perc)])
            doc.add_table(trimmomatic_table_part1, TABLE_WIDTH)
            doc.add_spacer()
            doc.add_table(trimmomatic_table_part2, TABLE_WIDTH)
            ##now save the trimmed reads
            trimmomaticDir = os.path.split(libraryInfo["trimmomatic"])[0]
            trimmomaticResultDir  = os.path.join(workingDir, "fastq_trimmed")
            if not os.path.exists(trimmomaticResultDir):
                os.makedirs(trimmomaticResultDir)
            filesToCopy = [os.path.join(trimmomaticDir, f) for f in \
                    os.listdir(trimmomaticDir) \
                    if (os.path.isfile(os.path.join(trimmomaticDir,f)) \
                    and re.search('.gz$',f))]
            for source in filesToCopy:
                dest = os.path.join("fastq_trimmed" , os.path.split(source)[1])
                if not os.path.isfile(dest):
                    shutil.copyfile(source, dest)

        if tool == "fastqc" and "fastqc" in sample_config:
            fastqc_dir = sample_config["fastqc"]
            for fastqc_run in [dir for dir in os.listdir(fastqc_dir) \
                    if os.path.isdir(os.path.join(fastqc_dir, dir))]:
                fastqc_run_dir = os.path.join(fastqc_dir, fastqc_run, "Images")
                doc.add_image(os.path.join(fastqc_run_dir,
                    "per_base_quality.png"), 400, 180, pdf.CENTER,
                    "{} -- Per Base Quality".format(fastqc_run))
                fastqc_run_dir = os.path.join(fastqc_dir, fastqc_run, "Images")
                doc.add_image(os.path.join(fastqc_run_dir,
                    "sequence_length_distribution.png"), 400, 180, pdf.CENTER,
                    "{} -- Sequence Length Distribution".format(fastqc_run))
            #If I have not yet copied fastqc results do it
            if not os.path.exists("fastqc"):
                dirsToBeCopied = [os.path.join(fastqc_dir, f) for f in \
                        os.listdir(fastqc_dir) \
                        if os.path.isdir(os.path.join(fastqc_dir, f))]
                for source in dirsToBeCopied:
                    dest = os.path.join("fastqc", os.path.split(source)[1])
                    if not os.path.exists(dest):
                        shutil.copytree(source, dest)

        if tool == "abyss" and "abyss" in sample_config:
            doc.add_paragraph("A possible way to assess the complexity of a "
                    "library even in absence of a reference sequence is to "
                    "look at the kmer profile of the reads. The idea is to "
                    "count all the kmers (i.e., sequence of length k) that occur "
                    "in the reads. In this way it is possible to know how many "
                    "kmers occur 1,2,..., N times and represent this as a "
                    "plot. This plot tell us for each x, how many k-mers "
                    "(y-axis) are present in the dataset in exactly x-copies. "
                    "In an ideal world (no errors in sequencing, no bias, no "
                    "repeating regions) this plot should be as close as "
                    "possible to a gaussian distribution. In reality we will "
                    "always see a peak for x=1 (i.e., the errors) and another "
                    "peak close to the expected coverage. If the genome is "
                    "highly heterozygous a second peak at half of the coverage "
                    "can be expected.")
            kmer_1_200 = os.path.join(sample_config["abyss"],
                    "kmer_coverage.png")
            doc.add_image(kmer_1_200, 500, 300, pdf.CENTER,
                    "kmer profile with k={}.".format(sample_config["kmer"]))
            #copy the results in resutls
            if not os.path.exists("kmer_analysis"):
                os.mkdir("kmer_analysis")
            kmerDir = sample_config["abyss"]
            filesToCopy = [os.path.join(kmerDir, f) for f in \
                    os.listdir(kmerDir) \
                    if (os.path.isfile(os.path.join(kmerDir,f)) \
                    and re.search('.png$',f))]
            filesToCopy.append(os.path.join(kmerDir, "histogram.hist"))
            for source in filesToCopy:
                dest = os.path.join("kmer_analysis", os.path.split(source)[1])
                if not os.path.exists(dest):
                    shutil.copyfile(source, dest)

        if tool == "align" and "alignments" in sample_config:
            alignments = sample_config["alignments"][0]
            alignment_path = alignments[1]
            alignment_prefix = alignments[2]
            align_dir = os.path.split(alignment_path)[0]
            doc.add_header("{} -- Collect Insert Size Metrics".format(
                sampleName) , pdf.H3)
            with open(os.path.join(align_dir,
                "{}.collectInsertSize.txt".format(alignment_prefix))) \
                as collectInsertSize:
                    lines  = collectInsertSize.readlines()
                    line  = lines[6].rstrip().split("\t")
                    # this is the header row
                    insertSize_table = [[line[7], line[6], line[4], line[5]]]
                    line  = lines[7].rstrip().split("\t")
                     # this is the header row
                    insertSize_table.append([line[7], line[6], line[4],
                        line[5]])
                    line  = lines[8].rstrip().split("\t")
                     # this is the header row
                    insertSize_table.append([line[7], line[6], line[4],
                        line[5]])
                    line  = lines[9].rstrip().split("\t")
                     # this is the header row
                    insertSize_table.append([line[7], line[6], line[4],
                        line[5]])
                    doc.add_table(insertSize_table, TABLE_WIDTH)
            doc.add_spacer()
            full_path_to_pdf =  os.path.join(align_dir,
                    "{}.collectInsertSize.pdf".format(alignment_prefix))
            doc.add_paragraph("Insert size plot can be found in the result \
                    directory: {}".format(os.path.join("alignments",
                    "{}.collectInsertSize.pdf".format(alignment_prefix))))
            doc.add_spacer()
            doc.add_header("{} -- Duplicate Metrics".format(sampleName),
                    pdf.H3)
            with open(os.path.join(align_dir,
                "{}.markDuplicates.txt".format(alignment_prefix))) as \
                collectInsertSize:
                    lines  = collectInsertSize.readlines()
                    line  = lines[6].rstrip().split("\t")
                    # this is the header row
                    duplication_table_part1 = [line[0:3]]
                    duplication_table_part2 = [line[4:6]]
                    duplication_table_part3 = [line[7:9]]
                    line  = lines[7].rstrip().split("\t")
                    duplication_table_part1.append(line[0:3])
                    duplication_table_part2.append(line[4:6])
                    duplication_table_part3.append(line[7:9])
            doc.add_table(duplication_table_part1, TABLE_WIDTH)
            doc.add_spacer()
            doc.add_table(duplication_table_part2, TABLE_WIDTH)
            doc.add_spacer()
            doc.add_table(duplication_table_part3, TABLE_WIDTH)
            doc.add_spacer()
            full_path_to_bam =  os.path.join(align_dir,
                    "{}_noDup.bam".format(alignment_prefix))
            doc.add_paragraph("Bam file with marked duplicate reads can be \
                    found at: {}".format(os.path.join("alignments",
                    "{}_noDup.bam".format(alignment_prefix))))
            doc.add_spacer()
            #copy the results in resutls
            if not os.path.exists("alignments"):
                os.mkdir("alignments")
            filesToCopy = [os.path.join(align_dir, f) for f in \
                    os.listdir(align_dir) \
                    if (os.path.isfile(os.path.join(align_dir,f)) \
                    and re.search('{}'.format(alignment_prefix),f))]
            for source in filesToCopy:
                dest = os.path.join("alignments", os.path.split(source)[1])
                if not os.path.exists(dest):
                    shutil.copyfile(source, dest)

        if tool == "kmergenie" and "kmergenie" in sample_config:
            doc.add_paragraph("Assemblers using a de Bruijn graph strategy "
                    "for contig construction (such as Velvet, ABySS and "
                    "SOAPdenovo) fractures the reads into k-sized substrings "
                    "(k-mers). The k-mer size is vital for the performance of "
                    "these assemblers, and is usually selected considering "
                    "several trade-offs between the size and accuracy of the "
                    "produced contigs. Some assemblers choose the k-mer size "
                    "automatically or builds several assemblies (using "
                    "different k-mers) and / or relies on user input. "
                    "Kmergenie is a lightweight program that suggests a best "
                    "k-mer size based on their relative abundance in the "
                    "genomic reads.")
            kmerdir = sample_config["kmergenie"]
            doc.add_image(os.path.join(kmerdir,"histograms.dat.png"), 400, 300, 
                    pdf.CENTER, ("The plot should be roughly concave and have "
                            "a clear global maximum, if not the predicted best "
                            "k is likely to be inaccurate"))
            #copy everything to results
            dest = os.path.join(os.getcwd(), "kmergenie")
            if not os.path.exists(dest):
                shutil.copytree(kmerdir, dest)
    doc.render(PDFtitle)

    ##TODO: if trimmomatic not run needs to copy also original reads?
    os.chdir(currentDir)
