# Incremental-SC

Introduction
-----
Here we develop a new yet practical annotation tool that aims to continuously learn new cell type knowledge from the data stream. 

Architecture
-----
![model](https://github.com/AimeeFreedom/Incremental-SC/blob/main/Architecture/framework.pdf)

Requirement
-----
The version of Python environment and packages we used can be summarized as follows,

python environment >=3.6

torch >=1.10.2

scanpy 1.4.4

scikit-learn 0.20.4

scipy 1.1.0

jgraph 0.2.1

tqdm 4.64.1

...

Please build the corresponding operation environment before running our codes.

Quickstart
-----
We provide some explanatory descriptions for the codes, please see the specific code files. We supply several kinds of training codes for intra-data, inter-tissue, and inter-data, respectively. If you want to use the learning strategies in the intra-data setting, you can focus on the "single" series, and if you want to use them in the inter-tissue and inter-data settings, you can pay attention to the "real" series. 

Data
-----
All datasets we used can be downloaded in <a href="https://cblast.gao-lab.org/download">data</a>.

Contributing
-----
Author email: zhaiyuyao@stu.pku.edu.cn. If you have any questions, please contact me.
