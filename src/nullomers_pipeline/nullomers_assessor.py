import sys
#import Bio
#import statsmodels
import math
import numpy as np
import pandas as pd
from collections import defaultdict
from collections import Counter
from time import process_time

# code from: https://github.com/gkoulouras/Nullomers-Assessor/blob/master/nullomers_assessor.py
########## main function ##############################################################################

# we will be using data that is in genomic format, so we need to convert it to RNA format
def convert_to_RNA(sequence):
    """ function that gets a sequence in DNA format and outputs its corresponding RNA sequence"""
    mapping = {
        'A': 'U',
        'G': 'C',
        'C': 'G',
        'T': 'A',
        'N': '_'
    }
    return ''.join(mapping.get(base, base) for base in sequence)


if __name__ == "__main__":
    fasta_file = sys.argv[1]
    nullomers_file = sys.argv[2]
    threshold = float(sys.argv[3])
    level = sys.argv[4]
    correction_method = sys.argv[5]
    print_log = sys.argv[6]
    output_file = sys.argv[7]
else:
    print("\n**An error occured**\n")
    raise SystemExit() 

if (correction_method != "FDR") and (correction_method != "TARONE") and (correction_method != "BONF"):
    print("\n**Please choose one of the following attributes for statistical correction method: BONF or TARONE or FDR**\n")
    raise SystemExit()

if (print_log != "TRUE") and (print_log != "FALSE"):
    print("\n**The 'print_log' parameter should be either TRUE or FALSE**\n")
    raise SystemExit()


if (level == "RNA"):
    alphabet = "ACGU_"
elif (level == "PROT"):
    alphabet = "ACDEFGHIKLMNPQRSTVWY"
else:
    print("\n**Please declare the type of sequences. Accepted attributes are RNA or PROT**\n")
    raise SystemExit()

#################################################################################################################


######### secondary functions ###################################################################################

def return_indices(array, value):
    return np.where(array==value)

def nth_order_probs(sequences, n):
    num = defaultdict(int)
    for seq in sequences:
        for i in range(len(seq) - n):
            j = i + n + 1
            key = seq[i:j]
            num[key] += 1
    denom = defaultdict(int)
    for key, value in (num.items()):
        denom[key[0:n]] += value
    return {key: value/denom[key[0:n]] for key, value in num.items()}

def _ecdf(x):
    nobs = len(x)
    return np.arange(1,nobs+1)/float(nobs)

def fdrcorrection(pvals, thresh, is_sorted=False):
    ###FDR correction -- more info at: http://www.statsmodels.org/devel/_modules/statsmodels/stats/multitest.html#multipletests
    pvals = np.asarray(pvals)

    if not is_sorted:
        pvals_sortind = np.argsort(pvals)
        pvals_sorted = np.take(pvals, pvals_sortind)
    else:
        pvals_sorted = pvals  # alias

    ecdffactor = _ecdf(pvals_sorted)
    reject = pvals_sorted <= ecdffactor*thresh
    if reject.any():
        rejectmax = max(np.nonzero(reject)[0])
        reject[:rejectmax] = True

    pvals_corrected_raw = pvals_sorted / ecdffactor
    pvals_corrected = np.minimum.accumulate(pvals_corrected_raw[::-1])[::-1]
    del pvals_corrected_raw
    pvals_corrected[pvals_corrected>1] = 1
    if not is_sorted:
        pvals_corrected_ = np.empty_like(pvals_corrected)
        pvals_corrected_[pvals_sortind] = pvals_corrected
        del pvals_corrected
        reject_ = np.empty_like(reject)
        reject_[pvals_sortind] = reject
        return reject_, pvals_corrected_
    else:
        return reject, pvals_corrected

#################################################################################################################

title = "\n***** Nullomers Assessor: Statistical evaluation of nullomers (version 1.04) *****\n"
if (print_log == 'TRUE'):
    print(title)

########## calculations ######################
    
start_time = process_time()

if (level == 'PROT') and (print_log == 'TRUE'):
    print("- The background proteome is currently processed")
elif (level == 'RNA') and (print_log == 'TRUE'):
    print("- The background genome is currently processed")

full_seq_list = []

full_seq_list = []
with open(fasta_file, "r") as fasta_file:
    sequence = ""
    for line in fasta_file:
        line = line.strip()
        if line.startswith(">"):
            if sequence:
                # convert file to corresponding RNA sequence
                sequence = convert_to_RNA(sequence)
                full_seq_list.append(str(sequence))
                sequence = ""
        else:
            sequence += line
    if sequence:
        # convert last sequence to corresponding RNA sequence
        sequence = convert_to_RNA(sequence)
        full_seq_list.append(str(sequence))
    
print('Finished parsing the fasta file')

if (print_log == 'TRUE'):
    print("- The calculation of transition probabilities is in progress")

aminoacid_percentage = nth_order_probs(full_seq_list, 0)

# Combine all sequences into one string
all_sequences = ''.join(full_seq_list)

# Count amino acids
aminoacid_counter = Counter(all_sequences)

# Total length of the sequence
total_sequence_length = sum(aminoacid_counter.values())

if (print_log == 'TRUE'):
    print("- The frequency of residues has been calculated successfully")

transition_dictionary_first_order = nth_order_probs(full_seq_list, 1)
if (print_log == 'TRUE'):
    print("- The 1st-order transition probabilities have been calculated successfully")
    
transition_dictionary_second_order = nth_order_probs(full_seq_list, 2)
if (print_log == 'TRUE'):  
    print("- The 2nd-order transition probabilities have been calculated successfully")

transition_dictionary_third_order = nth_order_probs(full_seq_list, 3)
if (print_log == 'TRUE'):   
    print("- The 3rd-order transition probabilities have been calculated successfully")


##empty full_seq_list to free up memory
full_seq_list.clear()

if (print_log == 'TRUE'):
    print("- The list of minimal absent words is currently processed")

line_length = {}
nullomer_list = []
exp_num_occur_zero_order = []
exp_num_occur_first_order = []
exp_num_occur_second_order = []
exp_num_occur_third_order = []
prob_corr_zero_occur_list_zero_order = []
prob_corr_zero_occur_list_first_order = []
prob_corr_zero_occur_list_second_order = []
prob_corr_zero_occur_list_third_order = []


with open(nullomers_file, encoding='utf8') as f:

    lines = f.read().splitlines()
    if (level == 'PROT'):
        lines = [ x for x in lines if x and (">" not in x) ] ##exclude blank lines and lines contain the '>' symbol
    elif (level == 'RNA'):
        lines = [ x for x in lines if x and (">" not in x) and ("_" not in x) ] ##exclude blank lines, lines that include 'N's and lines contain the '>' symbol
    
    if (print_log == 'TRUE'):        
        print("- The list of minimal absent words has been parsed successfully\n")
        print("- The assesment of minimal absent words is now starting")
        print("** Please be patient this might be a time-consuming process **")
   
    max_len = len(max(lines, key=len))
    min_len = len(min(lines, key=len))

    if (level == 'PROT') and (print_log == 'TRUE'):
        print("- The shortest peptide of the list is a " + str(min_len) + "-mer while the longest is a " + str(max_len) + "-mer")
    elif (level == 'RNA') and (print_log == 'TRUE'):
        print("- The shortest nucleotide sequence of the list is a " + str(min_len) + "-mer while the longest is a " + str(max_len) + "-mer")


    if (level == 'PROT' and max_len > 6):
        print("\n**Nullomers' chains should be up to 6 amino acids in length. Please try again with shorter sequences**\n")
        raise SystemExit()
    elif (level == 'RNA' and correction_method == 'FDR' and max_len > 18):
        print("\n**Nullomers' chains should be up to 18 nucleotides in length using FDR method. Please try again with shorter sequences**\n")
	    #raise SystemExit()
    elif (level == 'RNA' and correction_method == 'BONF' and max_len > 18):
        print("\n**Nullomers should be up to 18 nucleotides in length using BONF method. Please try again with shorter sequences**\n")
	    #raise SystemExit()
    elif (level == 'RNA' and correction_method == 'TARONE' and max_len > 14):
        print("\n**Nullomers should be up to 14 nucleotides in length using TARONE method. Please try again with shorter sequences**\n")
        #raise SystemExit()


    if (correction_method == 'FDR'):
        if (print_log == 'TRUE'):
            print("- The selected correction method is: " + str(correction_method) + "")
            print("- The q-value threshold is: " + str(threshold) + "\n")

        probability_zero_occurrence_zero_order = []
        probability_zero_occurrence_first_order = []
        probability_zero_occurrence_second_order = []
        probability_zero_occurrence_third_order = []
        pvalues_zero_order = np.ones(len(lines), dtype=int)

        for index in range(len(lines)):
            if (index % 1000000) == 0 and index != 0:
                if (print_log == 'TRUE'):
                    print(str(index) + ' rows have been parsed')
            
            motif_length = len(lines[index])

            probability_one_occurrence_zero_order = 1
            probability_one_occurrence_first_order = 1
            probability_one_occurrence_second_order = 1
            probability_one_occurrence_third_order = 1
            
            for ind, current_letter in enumerate(str(lines[index])):
                
                if (ind == 0):
                    probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                    probability_one_occurrence_first_order = probability_one_occurrence_first_order * aminoacid_percentage[str(current_letter)]
                    probability_one_occurrence_second_order = probability_one_occurrence_second_order * aminoacid_percentage[str(current_letter)]
                    probability_one_occurrence_third_order = probability_one_occurrence_third_order * aminoacid_percentage[str(current_letter)]
                    one_previous_letter = current_letter
                if (ind==1):
                    probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                    probability_one_occurrence_first_order = probability_one_occurrence_first_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                    probability_one_occurrence_second_order = probability_one_occurrence_second_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                    probability_one_occurrence_third_order = probability_one_occurrence_third_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                    two_previous_letters = one_previous_letter
                    one_previous_letter = current_letter
                if (ind==2):
                    probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                    probability_one_occurrence_first_order = probability_one_occurrence_first_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                    if transition_dictionary_second_order.get(str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) is None:
                        probability_one_occurrence_second_order = probability_one_occurrence_second_order * 1
                        probability_one_occurrence_third_order = probability_one_occurrence_third_order * 1
                    else:
                        probability_one_occurrence_second_order = probability_one_occurrence_second_order * transition_dictionary_second_order.get(str(two_previous_letters)+str(one_previous_letter)+str(current_letter))
                        probability_one_occurrence_third_order = probability_one_occurrence_third_order * transition_dictionary_second_order.get(str(two_previous_letters)+str(one_previous_letter)+str(current_letter))
                    three_previous_letters = two_previous_letters
                    two_previous_letters = one_previous_letter
                    one_previous_letter = current_letter
                if (ind>=3):
                    probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                    probability_one_occurrence_first_order = probability_one_occurrence_first_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))                    
                    if transition_dictionary_second_order.get(str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) is None:
                        probability_one_occurrence_second_order = probability_one_occurrence_second_order * 1
                    else:
                        probability_one_occurrence_second_order = probability_one_occurrence_second_order * transition_dictionary_second_order.get(str(two_previous_letters)+str(one_previous_letter)+str(current_letter))
                    if transition_dictionary_third_order.get(str(three_previous_letters) + str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) is None:
                        probability_one_occurrence_third_order = probability_one_occurrence_third_order * 1
                    else:
                        probability_one_occurrence_third_order = probability_one_occurrence_third_order * transition_dictionary_third_order.get(str(three_previous_letters) + str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) 
                    three_previous_letters = two_previous_letters
                    two_previous_letters = one_previous_letter
                    one_previous_letter = current_letter

            expected_number_occurrences_zero_order = probability_one_occurrence_zero_order * (total_sequence_length - motif_length + 1)
            expected_number_occurrences_first_order = probability_one_occurrence_first_order * (total_sequence_length - motif_length + 1)
            expected_number_occurrences_second_order = probability_one_occurrence_second_order * (total_sequence_length - motif_length + 1)
            expected_number_occurrences_third_order = probability_one_occurrence_third_order * (total_sequence_length - motif_length + 1)

            probability_zero_occurrence_zero_order.append(math.exp(-expected_number_occurrences_zero_order))
            probability_zero_occurrence_first_order.append(math.exp(-expected_number_occurrences_first_order))
            probability_zero_occurrence_second_order.append(math.exp(-expected_number_occurrences_second_order))
            probability_zero_occurrence_third_order.append(math.exp(-expected_number_occurrences_third_order))
        
        prob_corr_zero_occur_list_zero_order = fdrcorrection(probability_zero_occurrence_zero_order,threshold)
        prob_corr_zero_occur_list_first_order = fdrcorrection(probability_zero_occurrence_first_order,threshold)
        prob_corr_zero_occur_list_second_order = fdrcorrection(probability_zero_occurrence_second_order,threshold)
        prob_corr_zero_occur_list_third_order = fdrcorrection(probability_zero_occurrence_third_order,threshold)

        idx = return_indices(prob_corr_zero_occur_list_zero_order[0], True)
        idx1 = return_indices(prob_corr_zero_occur_list_first_order[0], True)
        idx2 = return_indices(prob_corr_zero_occur_list_second_order[0], True)
        idx3 = return_indices(prob_corr_zero_occur_list_third_order[0], True)
        
        ids_in_common = np.intersect1d(idx, idx1) 
        ids_in_common = np.intersect1d(ids_in_common, idx2)
        ids_in_common = np.intersect1d(ids_in_common, idx3)

      

        if ids_in_common.size:
            if (print_log == 'TRUE'):
                print("\n** Results **\n")
            # TODO: Make more generalizable !
            with open(output_file, "w") as fasta_file:
                fasta_file.write(f">nullomers_output\n")
                for index in ids_in_common:
                    print(str(lines[index]) + '\t' + str(max(prob_corr_zero_occur_list_zero_order[1][index], prob_corr_zero_occur_list_first_order[1][index], prob_corr_zero_occur_list_second_order[1][index], prob_corr_zero_occur_list_third_order[1][index])))
                    print(f"Writing results to {output_file}")
                    fasta_file.write(str(lines[index]+"\n"))
        else:        
            print("No significant results found")
            
######################

    elif (correction_method == 'BONF'): ##bonferroni correction
        if (print_log == 'TRUE'):
            print("- The selected correction method is: " + str(correction_method) + "")
            print("- The q-value threshold is: " + str(threshold) + "\n")
        
        for index in range(len(lines)):
            if (index % 1000000) == 0 and index != 0:
                if (print_log == 'TRUE'):
                    print(str(index) + ' rows have been parsed')             
            if not ">" in str(lines[index]) and len(str(lines[index]))!=0:
                motif_length = len(lines[index])

                probability_one_occurrence_zero_order = 1
                probability_one_occurrence_first_order = 1
                probability_one_occurrence_second_order = 1
                probability_one_occurrence_third_order = 1
            
                for ind, current_letter in enumerate(str(lines[index])):
                    if (ind == 0):
                        probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                        probability_one_occurrence_first_order = probability_one_occurrence_first_order * aminoacid_percentage[str(current_letter)]
                        probability_one_occurrence_second_order = probability_one_occurrence_second_order * aminoacid_percentage[str(current_letter)]
                        probability_one_occurrence_third_order = probability_one_occurrence_third_order * aminoacid_percentage[str(current_letter)]
                        one_previous_letter = current_letter
                    if (ind==1):
                        probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                        probability_one_occurrence_first_order = probability_one_occurrence_first_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                        probability_one_occurrence_second_order = probability_one_occurrence_second_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                        probability_one_occurrence_third_order = probability_one_occurrence_third_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                        two_previous_letters = one_previous_letter
                        one_previous_letter = current_letter
                    if (ind==2):
                        probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                        probability_one_occurrence_first_order = probability_one_occurrence_first_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))
                        if transition_dictionary_second_order.get(str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) is None:
                            probability_one_occurrence_second_order = probability_one_occurrence_second_order * 1
                            probability_one_occurrence_third_order = probability_one_occurrence_third_order * 1
                        else:
                            probability_one_occurrence_second_order = probability_one_occurrence_second_order * transition_dictionary_second_order.get(str(two_previous_letters)+str(one_previous_letter)+str(current_letter))
                            probability_one_occurrence_third_order = probability_one_occurrence_third_order * transition_dictionary_second_order.get(str(two_previous_letters)+str(one_previous_letter)+str(current_letter))
                        three_previous_letters = two_previous_letters
                        two_previous_letters = one_previous_letter
                        one_previous_letter = current_letter
                    if (ind>=3):
                        probability_one_occurrence_zero_order = probability_one_occurrence_zero_order * aminoacid_percentage[str(current_letter)]
                        probability_one_occurrence_first_order = probability_one_occurrence_first_order * transition_dictionary_first_order.get(str(one_previous_letter)+str(current_letter))                    
                        if transition_dictionary_second_order.get(str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) is None:
                            probability_one_occurrence_second_order = probability_one_occurrence_second_order * 1
                        else:
                            probability_one_occurrence_second_order = probability_one_occurrence_second_order * transition_dictionary_second_order.get(str(two_previous_letters)+str(one_previous_letter)+str(current_letter))
                        if transition_dictionary_third_order.get(str(three_previous_letters) + str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) is None:
                            probability_one_occurrence_third_order = probability_one_occurrence_third_order * 1
                        else:
                            probability_one_occurrence_third_order = probability_one_occurrence_third_order * transition_dictionary_third_order.get(str(three_previous_letters) + str(two_previous_letters) + str(one_previous_letter) + str(current_letter)) 
                        three_previous_letters = two_previous_letters
                        two_previous_letters = one_previous_letter
                        one_previous_letter = current_letter

                expected_number_occurrences_zero_order = probability_one_occurrence_zero_order * (total_sequence_length - motif_length + 1)
                expected_number_occurrences_first_order = probability_one_occurrence_first_order * (total_sequence_length - motif_length + 1)
                expected_number_occurrences_second_order = probability_one_occurrence_second_order * (total_sequence_length - motif_length + 1)
                expected_number_occurrences_third_order = probability_one_occurrence_third_order * (total_sequence_length - motif_length + 1)

                probability_zero_occurrence_zero_order = math.exp(-expected_number_occurrences_zero_order)
                probability_zero_occurrence_first_order = math.exp(-expected_number_occurrences_first_order)
                probability_zero_occurrence_second_order = math.exp(-expected_number_occurrences_second_order)
                probability_zero_occurrence_third_order = math.exp(-expected_number_occurrences_third_order)
                
		## statistical correction step begins here ##
                if (level == 'PROT'):
                    corrected_probability_zero_occurrence_zero_order = (probability_zero_occurrence_zero_order * pow(20, len(str(lines[index]))))
                    corrected_probability_zero_occurrence_first_order = (probability_zero_occurrence_first_order * pow(20, len(str(lines[index]))))
                    corrected_probability_zero_occurrence_second_order = (probability_zero_occurrence_second_order * pow(20, len(str(lines[index]))))
                    corrected_probability_zero_occurrence_third_order = (probability_zero_occurrence_third_order * pow(20, len(str(lines[index]))))
                elif (level == 'RNA'):
                    corrected_probability_zero_occurrence_zero_order = (probability_zero_occurrence_zero_order * pow(4, len(str(lines[index]))))
                    corrected_probability_zero_occurrence_first_order = (probability_zero_occurrence_first_order * pow(4, len(str(lines[index]))))
                    corrected_probability_zero_occurrence_second_order = (probability_zero_occurrence_second_order * pow(4, len(str(lines[index]))))
                    corrected_probability_zero_occurrence_third_order = (probability_zero_occurrence_third_order * pow(4, len(str(lines[index]))))
                
            
                if ((corrected_probability_zero_occurrence_zero_order < threshold) and (corrected_probability_zero_occurrence_first_order < threshold)  and (corrected_probability_zero_occurrence_second_order < threshold) and (corrected_probability_zero_occurrence_third_order < threshold)):
                    nullomer_list.append(str(lines[index]))
                    exp_num_occur_zero_order.append(expected_number_occurrences_zero_order)
                    exp_num_occur_first_order.append(expected_number_occurrences_first_order)
                    exp_num_occur_second_order.append(expected_number_occurrences_second_order)
                    exp_num_occur_third_order.append(expected_number_occurrences_third_order)
                
                    prob_corr_zero_occur_list_zero_order.append(corrected_probability_zero_occurrence_zero_order)
                    prob_corr_zero_occur_list_first_order.append(corrected_probability_zero_occurrence_first_order)
                    prob_corr_zero_occur_list_second_order.append(corrected_probability_zero_occurrence_second_order)
                    prob_corr_zero_occur_list_third_order.append(corrected_probability_zero_occurrence_third_order)
                
                
        if not (nullomer_list):    
            print("No significant results found")
        else:
            if (print_log == 'TRUE'):
                print("\n** Results **\n")
            # TODO: Make more generalizable !
            with open(output_file, "w") as fasta_file:
                fasta_file.write(f">nullomers_output\n")
                for itm1, itm2, itm3, itm4, itm5 in zip(nullomer_list, prob_corr_zero_occur_list_zero_order, prob_corr_zero_occur_list_first_order, prob_corr_zero_occur_list_second_order, prob_corr_zero_occur_list_third_order):
                    max_prob = max(itm2, itm3, itm4, itm5)
                    print(str(itm1) + '\t' + str(max_prob))
                    print(f"Writing results to {output_file}")
                    fasta_file.write(f"{itm1}\n")

#####################
        
    
    elif (correction_method == 'TARONE'): ##tarone (modified bonferroni) method
        if (print_log == 'TRUE'):
            print("- The selected correction method is: " + str(correction_method) + "")
            print("- The q-value threshold is: " + str(threshold) + "\n")
    
        default_transition_prob = 0.0  # !!!
        for i in product(alphabet, repeat=2):
            transition_dictionary_first_order.setdefault(''.join(i), default_transition_prob)
        for i in product(alphabet, repeat=3):
            transition_dictionary_second_order.setdefault(''.join(i), default_transition_prob)
        for i in product(alphabet, repeat=4):
            transition_dictionary_third_order.setdefault(''.join(i), default_transition_prob)

        remaining_kmers = {}
        for cur_len in range(min_len,max_len + 1):
            if (print_log == 'TRUE'): 
                print(str(cur_len) + '-mers are now evaluated')
            aa_combinations = product(alphabet, repeat=cur_len)
            promising_kmers_list = []
            range1 = range(cur_len - 1)
            range2 = range(cur_len - 2)
            range3 = range(cur_len - 3)
            number_of_kmer_positions = total_sequence_length - cur_len + 1
            for indexx, cur_comb in enumerate(aa_combinations):
                cur_comb = ''.join(cur_comb)
                if (indexx % 1000000) == 0 and indexx != 0:
                    if (print_log == 'TRUE'):
                        print(str(indexx) + ' rows have been parsed')

                pre_probability_one_occurrence_zero_order = 1
                pre_probability_one_occurrence_first_order = 1
                pre_probability_one_occurrence_second_order = 1
                pre_probability_one_occurrence_third_order = 1

                if cur_len > 0:
                    p = aminoacid_percentage[cur_comb[0]]
                    pre_probability_one_occurrence_first_order *= p
                    pre_probability_one_occurrence_second_order *= p
                    pre_probability_one_occurrence_third_order *= p
                if cur_len > 1:
                    p = transition_dictionary_first_order[cur_comb[:2]]
                    pre_probability_one_occurrence_second_order *= p
                    pre_probability_one_occurrence_third_order *= p
                if cur_len > 2:
                    p = transition_dictionary_second_order[cur_comb[:3]]
                    pre_probability_one_occurrence_third_order *= p

                for cur_let in cur_comb:
                    pre_probability_one_occurrence_zero_order *= aminoacid_percentage[cur_let]
                for i in range1:
                    pre_probability_one_occurrence_first_order *= transition_dictionary_first_order[cur_comb[i:i+2]]
                for i in range2:
                    pre_probability_one_occurrence_second_order *= transition_dictionary_second_order[cur_comb[i:i+3]]
                for i in range3:
                    pre_probability_one_occurrence_third_order *= transition_dictionary_third_order[cur_comb[i:i+4]]

                if (cur_len >= 5):
                    min_prob = min(pre_probability_one_occurrence_zero_order, pre_probability_one_occurrence_first_order, pre_probability_one_occurrence_second_order, pre_probability_one_occurrence_third_order)
                elif (cur_len == 4):
                    min_prob = min(pre_probability_one_occurrence_zero_order, pre_probability_one_occurrence_first_order, pre_probability_one_occurrence_second_order)
                elif (cur_len == 3):
                    min_prob = min(pre_probability_one_occurrence_zero_order, pre_probability_one_occurrence_first_order)
                elif (cur_len == 2):
                    min_prob = min(pre_probability_one_occurrence_zero_order)

                min_exp_numb_occurrences = min_prob * number_of_kmer_positions
                max_prob_zero_occurrence = math.exp(-min_exp_numb_occurrences)

                promising_kmers_list.append((max_prob_zero_occurrence, cur_comb))


            promising_kmers_list.sort(reverse = True)
            num_of_included_kmers = len(promising_kmers_list)
            for value_prob, key_nullo in promising_kmers_list:
            
                corr_prob = value_prob * num_of_included_kmers
            
                if corr_prob > threshold:
                    num_of_included_kmers -= 1
                else:   
                    remaining_kmers[key_nullo] = corr_prob


        keys = remaining_kmers.keys() & lines
        results = {k:remaining_kmers[k] for k in keys}
        if (print_log == 'TRUE'):
            print("\n** Results **\n")
        if len(results.keys()) == 0:
            print('No significant results found')
        else:
            # TODO: Make more generalizable !
            with open(output_file, "w") as fasta_file:
                print(f"Writing results to {output_file}")
                fasta_file.write(f">nullomers_output\n")
                for key, val in results.items():
                    print(str(key) + "\t" + str(val))
                    nullomer = str(key)
                    fasta_file.write(f"{nullomer}\n")


end_time = process_time() - start_time

if (print_log == 'TRUE'):
    print('\n\nTotal execution time: ' + str(end_time) + " seconds")  
    print("\n** Execution was successful **")