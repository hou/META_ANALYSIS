#!/usr/bin/env python3
import argparse
import json
import gzip
from collections import namedtuple
import sys
import math
from scipy.stats import chi2
import scipy.stats
import numpy
from typing import Dict, Tuple, List
import subprocess
from collections import deque

chrord = { "chr"+str(chr):int(chr) for chr in list(range(1,23))}
chrord["chrX"] = 23
chrord["chrY"] = 24
chrord["chrMT"] = 25
chrord.update({str(chr):int(chr) for chr in list(range(1,23)) } )

def n_meta( studies : List[Tuple['Study','MetaDat']] ):
    effs_size = []
    tot_size =0
    for s in studies:
        study = s[0]
        dat = s[1]
        effs_size.append( math.sqrt(study.effective_size) * numpy.sign(dat.beta) * dat.z_score)
        tot_size+=study.effective_size

    return scipy.stats.norm.sf( abs( sum( effs_size ) ) / math.sqrt(tot_size) )

def inv_var_meta( studies : List[Tuple['Study','MetaDat']] ):

    effs_inv_var = []
    tot_se = 0
    for s in studies:
        study = s[0]
        dat = s[1]

        if dat.se is None or dat.se==0:
            print("Standard error was none/zero for variant " + str(dat) + " in study " + study.name, file=sys.stderr)
            break
        var = (dat.se * dat.se)

        tot_se+=1/var
        effs_inv_var.append(var *  dat.beta )

    return scipy.stats.norm.sf(abs(sum(effs_inv_var) / math.sqrt(tot_se) )) if len(effs_inv_var)==len(studies) else None

def variance_weight_meta( studies : List[Tuple['Study','MetaDat']] ):
    effs_se = []
    tot_se = 0

    for s in studies:
        study = s[0]
        dat = s[1]

        if dat.se is None or dat.se==0:
            print("Standard error was none/zero for variant " + str(dat) + " in study " + study.name, file=sys.stderr)
            break

        effs_se.append( (1/dat.se) * numpy.sign(dat.beta) * dat.z_score )
        tot_se+=1/ (dat.se * dat.se)
    return scipy.stats.norm.sf( abs( sum( effs_se ) ) /  math.sqrt(tot_se)) if len(effs_se)==len(studies) else None


SUPPORTED_METHODS = {"n":n_meta,"inv_var":inv_var_meta,"variance":variance_weight_meta}

def check_eff_field(field):
    if field.lower() in ["beta","or"]:
        return field.lower()
    else:
        raise Exception("effect_type must be beta or OR")

flip = {"A":"T","C":"G","T":"A","G":"C"}

def flip_strand( allele):
    return "".join([ flip[a] for a in allele])

def is_symmetric(a1, a2):
    return (a1=="A" and a2=="T") or (a1=="T" and a2=="A") or (a1=="C" and a2=="G") or (a1=="G" and a2=="C")



class MetaDat:

    def __init__(self, chr, pos, ref, alt, beta, pval, se=None, extra_cols=[]):
        self.chr = chr
        self.pos = int(pos)
        self.ref = ref.strip()
        self.alt = alt.strip()
        self.beta = beta
        self.pval = pval
        self.z_scr = None
        try:
            self.se = float(se) if se is not None  else None
        except ValueError:
            self.se = None

        self.extra_cols = extra_cols
    def __eq__(self, other):

        return self.chr == other.chr and self.pos == other.pos and self.ref == other.ref and self.alt == other.alt

    def equalize(self, other:'MetaDat') -> bool:
        """
            Checks if this metadata is the same variant (possibly different strand or ordering of alleles)
            returns: true if the same (flips effect direction and ref/alt alleles if necessary) or false if not the same variant
        """

        if (self.chr == other.chr and self.pos == other.pos):
            flip_ref =  flip_strand(other.ref)
            flip_alt =  flip_strand(other.alt)


            if is_symmetric( other.ref, other.alt ):
                ## never strandflip symmetrics
                if self.ref == other.ref and self.alt == other.alt:
                    return True
                elif self.ref == other.alt and self.alt == other.ref:
                    self.beta = -1 * self.beta if self.beta is not None else None
                    t = self.alt
                    self.alt = self.ref
                    self.ref = t
                    return True

            elif( (self.ref == other.ref or self.ref==flip_ref) and (self.alt == other.alt or self.alt == flip_alt)):
                return True
            elif (self.ref == other.alt or self.ref == flip_alt) and (self.alt == other.ref or self.alt==flip_alt ) :
                self.beta = -1 * self.beta if self.beta is not None else None
                t = self.alt
                self.alt = self.ref
                self.ref = t
                return True
            else:
                return False

    @property
    def z_score(self):
        '''
            Lazy compute unsigned z-score
        '''
        if self.z_scr is None:
            self.z_scr = math.sqrt(chi2.isf(self.pval, df=1))
        return self.z_scr

    def __str__(self):
        return "chr:{} pos:{} ref:{} alt:{} beta:{} pval:{} se:{} ".format(self.chr, self.pos, self.ref, self.alt, self.beta, self.pval, self.se)


class Study:
    REQUIRED_DATA_FIELDS = {"chr":str,"pos":str,"ref":str,"alt":str, "effect":str,
    "pval":str}

    REQUIRED_CONF = {"name":str,"file":str, "n_cases": int, "n_controls":int,
    "chr":str,"pos":str,"ref":str,"alt":str, "effect":str,
    "effect_type":check_eff_field,
    "pval":str}

    OPTIONAL_FIELDS = {"se":str}

    def __init__(self, conf):
        self.conf =conf
        self.future = deque()
        self.eff_size= None
        self.z_scr = None
        for v in Study.REQUIRED_CONF:
            if v not in self.conf:
                raise Exception("Meta configuration for study must contain required elements: "
                    + ",".join(Study.REQUIRED_CONF.keys() ) + ". Offending configuration: " + str(s))

            try:
                self.conf[v] = Study.REQUIRED_CONF[v](self.conf[v])
            except Exception as e:
                raise Exception("Illegal data type in configuration for field " + s[v] +
                    " in configuration: " + str(s) + ". ERR:" + str(e))

        for v in Study.OPTIONAL_FIELDS:
            if v not in self.conf:
                continue
            try:
                self.conf[v] = Study.OPTIONAL_FIELDS[v](self.conf[v])
            except Exception as e:
                raise Exception("Illegal data type in configuration for field " + s[v] +
                    " in configuration: " + str(s) + ". ERR:" + str(e))

        self.conf["fpoint"] = gzip.open(conf["file"],'rt')
        header = conf["fpoint"].readline().rstrip().split("\t")

        for k in Study.REQUIRED_DATA_FIELDS.keys():
            if self.conf[k] not in header:
                raise Exception("Required headers not in data in study " + self.conf["name"] + ". Missing:" + ",".join([ self.conf[k] for k in Study.REQUIRED_DATA_FIELDS.keys() if self.conf[k] not in header])  )
        self.conf["h_idx"] = { k:header.index( self.conf[k] ) for k in Study.REQUIRED_DATA_FIELDS.keys() }

        for f in Study.OPTIONAL_FIELDS.keys():
            if f in self.conf:
                 if self.conf[f] not in header:
                     raise Exception("Configured column " + self.conf[f] + " not found in the study results " + self.conf["name"])
                 self.conf["h_idx"][f] = header.index(self.conf[f])

        if "extra_cols" in self.conf:
            for c in self.conf["extra_cols"]:
                if c not in header:
                    raise Exception("Configured column " + self.conf[c] + " not found in the study results " + self.conf["name"])
                self.conf["h_idx"][c] = header.index(c)
        else:
             self.conf["extra_cols"] = []



    @property
    def n_cases(self):
        return self.conf["n_cases"]

    @property
    def n_controls(self):
        return self.conf["n_cases"]

    @property
    def effective_size(self):
        if self.eff_size is None:
            self.eff_size = ( (4 * self.n_cases *  self.n_controls  ) / ( self.n_cases+  self.n_controls ))
        return self.eff_size
    @property
    def name(self):
        return self.conf["name"]

    def has_std_err(self):
        return "se" in self.conf

    def get_next_data(self, just_one =False) -> List[MetaDat]:
        """
            Returns a list of variants. List containts >1 elements if they are on the same position and just_one ==False.
            args:
                just_one: always returns only the next variant in order and not all next with the same position
            returns: list of next variants
        """
        if len(self.future)>0:
            ## only return variants with same position so that possible next variant position stored stays
            f = [ (i,v) for i,v in enumerate(self.future) if i==0 or (v.chr==self.future[i-1].chr and  v.pos==self.future[i-1].pos) ]
            for i,v in reversed(f):
                 del self.future[i]
            return [ v for i,v in f ]

        vars = list()
        while True:
            chr = None
            l = None
            ## loop ignoring  alternate contigs for now.
            while chr is None or chr not in chrord:
                l = self.conf["fpoint"].readline()
                if l=="":
                    return None

                l = l.rstrip().split("\t")
                chr = l[self.conf["h_idx"]["chr"]]

            pos = l[self.conf["h_idx"]["pos"]]
            ref = l[self.conf["h_idx"]["ref"]]
            alt = l[self.conf["h_idx"]["alt"]]
            eff = l[self.conf["h_idx"]["effect"]]
            pval = l[self.conf["h_idx"]["pval"]]

            pos = int(pos)

            se = l[self.conf["h_idx"]["se"]] if "se" in self.conf["h_idx"] else None

            effect_type = self.conf["effect_type"]
            try:
                pval = float(pval)
                eff = float(eff)
            except Exception as e:
                pval = None
                eff = None

            if( effect_type=="or" and eff):
                eff = math.log(eff)

            chr = chrord[chr]
            extracols = [ l[self.conf["h_idx"][c]] for c in self.conf["extra_cols"] ]

            v = MetaDat(chr,pos,ref,alt, eff, pval, se, extracols)

            if len(vars)==0 or ( vars[0].chr == v.chr and vars[0].pos == v.pos  ):
                vars.append(v )
                if just_one:
                    break
            else:
                self.future.append(v )
                break

        return vars

    @property
    def extra_cols(self):
        return self.conf["extra_cols"]

    def get_match(self, dat: MetaDat) -> MetaDat:
        """
            Reads current study until variant in 'dat' is reached or overtaken in chr pos orded.
            IF matching variant found (can flip alleles) the matching MetaDat(effect flipped if alleles flipped) is returned.
            input:
                dat: the variant to look for
            output: matching MetaDat in this study or None if no match.
        """

        otherdats = self.get_next_data( )

        if otherdats is None or len(otherdats)==0:
            return None

        while otherdats is not None and (otherdats[0].chr<dat.chr or (otherdats[0].chr==dat.chr and otherdats[0].pos<dat.pos)):
            otherdats = self.get_next_data()

        if otherdats is None:
            return None

        if otherdats[0].chr > dat.chr or otherdats[0].pos> dat.pos:
            self.put_back(otherdats)
            return None

        for i,v in enumerate(otherdats):
            if v.equalize(dat):
                del otherdats[i]
                self.put_back(otherdats)
                return v

        ## no match but stayed in the same pos. add variants back to future queue
        self.put_back(otherdats)
        return None


    def put_back(self, metadat):
        for m in metadat:
            ## the future in next position will be always kept last
            self.future.appendleft(m)


def get_studies(conf:str) -> List[Study]:
    """
        Reads json configuration and returns studies in the meta
    """

    studies_conf = json.load(open(conf,'r'))
    std_list = studies_conf["meta"]

    return [ Study(s) for s in studies_conf["meta"]]

def do_meta(study_list: List[ Tuple[Study, MetaDat]], methods: List[str] ) -> List[float] :
    '''
        Computes meta-analysis between all studies and data given in the std_list
        input:
            study_list: studies and data in tuples
        output:
            tuple in which first element is effective sample size weighted meta, second is std err weighted meta and 3rd is inverse variance weighted meta.
            2nd and 3rd elements are none if all studies did not have optional std err defined
    '''
    return [ SUPPORTED_METHODS[m](study_list) for m in methods ]

def format_num(num, precision=2):
    return numpy.format_float_scientific(num, precision=precision) if num is not None else "NA"

def run():
    '''
        This module generates matrix from external single association results for fast access to browsingself.
        First parameter should be a path to a json configuration file with these elements:
            "name":"FINNGEN",
            "file":"/Users/mitja/projects/finngen/META_ANALYSIS/I9_AF.gz",
            "n_cases": 6570 ,
            "n_controls": 48378,
            "chr":"CHR",
            "pos":"POS",
            "ref":"Allele1",
            "alt":"Allele2",
            "effect":"BETA",
            "effect_type":"beta",
            "pval":"p.value"
            "se":"SE" <- this parameter is optional. If given for compared studies additional p-value will be added using this as a weight for z-score.
        Second parameter should be a path to (empty/not existing) directory where the data should be stored
    '''

    parser = argparse.ArgumentParser(description="Create tabixed big matrix for external results")
    parser.add_argument('config_file', action='store', type=str, help='Configuration file ')
    parser.add_argument('path_to_res', action='store', type=str, help='Result file')

    parser.add_argument('methods', action='store', type=str, help='List of meta-analysis methods to compute separated by commas.'
            + 'Allowed values [n,inv_var,variance]', default="inv_var")

    args = parser.parse_args()

    studs = get_studies(args.config_file)

    methods = []


    for m in args.methods.split(","):
        if m not in SUPPORTED_METHODS:
            raise Exception("Unsupported meta method" + m + " given. Supported values" + ",".join(SUPPORTED_METHODS))
        methods.append(m)

    if "inv_var" in methods or "variance" in methods:
        for s in studs:
            if not s.has_std_err():
                raise Exception("Variance based method requested but not all studies have se column specified.")

    outfile = args.path_to_res

    with open( outfile, 'w' ) as out:

        out.write("\t".join(["#CHR","POS","REF","ALT", studs[0].name + "_beta", studs[0].name + "_pval"  ]))

        out.write( ("\t" if len(studs[0].extra_cols) else "") + "\t".join( [studs[0].name + "_" + c for c in studs[0].extra_cols] ) )
        ## align to leftmost STUDY
        for oth in studs[1:len(studs)]:
            out.write( "\t" +  "\t".join( [ oth.name + "_beta", oth.name + "_pval"] ))
            out.write( ("\t" if len(oth.extra_cols) else "") + "\t".join( [oth.name + "_" + c for c in oth.extra_cols] ) )

            for m in methods:
                out.write("\t" + studs[0].name + "_" + oth.name + "_" +  m + "_meta_p")

        out.write("\tall_meta_N")
        for m in methods:
            out.write("\tall_"+  m +"_meta_p")
        out.write("\n")

        d = studs[0].get_next_data(just_one = True)
        while d is not None:

            ## only one variant read at a time from matched study
            d = d[0]
            matching_studies = [ (studs[0],d) ]
            outdat = [ d.chr, d.pos, d.ref, d.alt,format_num(d.beta), format_num(d.pval)  ]
            outdat.extend([ c for c in d.extra_cols ])
            for oth in studs[1:len(studs)]:
                match_dat = oth.get_match(d)

                if match_dat is not None:
                    matching_studies.append( (oth,match_dat) )
                    met = do_meta( [(studs[0],d), (oth,match_dat)], methods=methods )
                    outdat.extend([format_num(match_dat.beta), format_num(match_dat.pval) ])
                    outdat.extend([ c for c in match_dat.extra_cols ])

                    for m in met:
                        outdat.append(format_num(m))
                else:
                    outdat.extend(['NA']  * (2 + len(oth.extra_cols) + len(methods) ) )

            if len(matching_studies)>1:
                met = do_meta( matching_studies, methods=methods )
                outdat.append( str(len(matching_studies)) )

                for m in met:
                    outdat.append( format_num(m) )

            else:
                outdat.extend(["NA"] *  len(methods) )

            out.write( "\t".join([ str(o) for o in outdat]) + "\n" )

            d = studs[0].get_next_data(just_one = True)

    subprocess.run(["bgzip","--force",args.path_to_res])
    subprocess.run(["tabix","-s 1","-b 2","-e 2",args.path_to_res + ".gz"])


if __name__ == '__main__':
    run()
