FROM chuyuanliu/heptools:base

RUN mamba env update -n hep -f https://raw.githubusercontent.com/chuyuanliu/heptools/master/docker/ml-cpu.yml \
    && mamba clean --all --yes
ENTRYPOINT ["tini", "-g", "--"]