# NOTE: the script is supposed to be used called from nnsvs recipes.
# Please don't try to run the shell script directory.

if [ -d conf/train ]; then
    ext="--config-dir conf/train/duration"
else
    ext=""
fi

if [ ! -z "${pretrained_expdir}" ]; then
    resume_checkpoint=$pretrained_expdir/duration/latest.pth
else
    resume_checkpoint=
fi
xrun nnsvs-train $ext \
    model=$duration_model train=$duration_train data=$duration_data \
    data.train_no_dev.in_dir=$dump_norm_dir/$train_set/in_duration/ \
    data.train_no_dev.out_dir=$dump_norm_dir/$train_set/out_duration/ \
    data.dev.in_dir=$dump_norm_dir/$dev_set/in_duration/ \
    data.dev.out_dir=$dump_norm_dir/$dev_set/out_duration/ \
    data.in_scaler_path=$dump_norm_dir/in_duration_scaler.joblib \
    data.out_scaler_path=$dump_norm_dir/out_duration_scaler.joblib \
    train.out_dir=$expdir/duration \
    train.log_dir=tensorboard/${expname}_duration \
    train.resume.checkpoint=$resume_checkpoint