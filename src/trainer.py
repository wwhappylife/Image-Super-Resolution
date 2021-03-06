import os
import time
import datetime
import numpy as np
from utils import Saver
from eval import calc_score

import torch
from torch.nn import init
from torch.autograd import Variable
from torchvision.transforms import ToPILImage


def backup_codes(args):
    import shutil
    import glob
    out_dir = os.path.join(args.save_dir, args.exp_name, 'codes')
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    pyfiles = glob.glob("./*.py")
    for pf in pyfiles:
        shutil.copy2(pf, out_dir)


def restore(args, model):
    # load
    saver = Saver(args)
    return saver.load(model)


def print_lr(optimizer):
    for param_group in optimizer.param_groups:
        print(param_group['lr'])


def network_paras(model):
    # compute only trainable
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    return params


def run_benchmark(args, model, dir_benchmark, out_dir='default', is_compare=True):
    import pandas as pd
    from data import BenchmarkSet

    # mkdir
    if out_dir == 'default':
        out_dir = os.path.join(args.save_dir, args.exp_name, 'benchmark_result')
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    # misc
    benchmarks = ['Set5', 'Set14', 'BSD100', 'Urban100']
    scale_str = 'image_SRF_' + str(args.scale)
    result = {'psnr': [], 'ssim': []}

    # start running
    print('{:=^40}'.format(' testing benchmarks '))
    for benchmark in benchmarks:
        print('[%s]' % benchmark)

        # foler arrangement
        root = os.path.join(dir_benchmark, benchmark, scale_str)
        save_dir = os.path.join(out_dir, root[len(dir_benchmark)+1:])
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        # get set
        benchmark_set = BenchmarkSet(args, root)

        # testing
        psnr, ssim = test(args, model, benchmark_set, out_dir=out_dir)

        # append
        result['psnr'].append(psnr)
        result['ssim'].append(ssim)

        if is_compare:
            data_frame = pd.read_csv(os.path.join(dir_benchmark, benchmark+'.csv'))
            new_row = pd.Series({'psnr': psnr, 'ssim': ssim, 'method': args.exp_name}, name='new')
            data_frame = data_frame.append(new_row)
            data_frame.to_csv(os.path.join(out_dir, benchmark+'.csv'))


def test(args, model, test_set, out_dir='default', is_save=True):
    test_loader = torch.utils.data.DataLoader(
        test_set,
        batch_size=1,
        shuffle=False,
        num_workers=int(args.num_threads))

    # mkdir
    if out_dir == 'default':
        out_dir = os.path.join(args.save_dir, args.exp_name, 'testing_result')
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    # misc
    total_psnr = 0.0
    total_ssim = 0.0
    flag_loss = False
    num_img = test_set.__len__()

    # ensurance
    args.need_patch = False
    model.cuda()
    model.eval()

    # start testing
    print('{:=^40}'.format(' testing start '))
    time_start = time.time()
    with torch.no_grad():
        for idx, (fn, im_lr, im_hr) in enumerate(test_loader):
            # forward
            im_lr = Variable(im_lr.cuda(), volatile=False)
            output = model(im_lr)

            # clip value range [0.0, 1.0]
            output = torch.clamp(output, min=0.0, max=1.0).cpu()

            # to PIL
            pil = ToPILImage()(torch.squeeze(output, 0))
            pil_lr = ToPILImage()(torch.squeeze(im_lr.cpu(), 0))
            print('(%d/%d) size: %s -> %s' % (
                                           idx,
                                           num_img,
                                           'x'.join(map(str, list(pil_lr.size))),
                                           'x'.join(map(str, list(pil.size)))))

            # save PIL
            if out_dir is not None:
                out_path = os.path.join(out_dir, fn[0])
                print(' =>  %s' % out_path)
                pil.save(out_path)

            # compute loss
            if im_hr is not None:
                flag_loss = True
                im_hr = Variable(im_hr.cuda())
                psnr, ssim = calc_score(output, im_hr)
                total_psnr += psnr
                total_ssim += ssim
                print('    psnr: %.5f, ssim: %.5f\n' % (psnr, ssim))

    print('{:=^40}'.format(' Finish '))
    runtime = time.time() - time_start
    print('testing time:', str(datetime.timedelta(seconds=runtime))+'\n')

    # record results
    if flag_loss:
        psnr_mean = total_psnr/num_img
        ssim_mean = total_ssim/num_img
        log = 'psnr: %.6f\nssim: %.6f\n' % (psnr_mean, ssim_mean)
        print(log)
        if is_save:
            log_file = os.path.join(out_dir, 'eval_result.txt')
            with open(log_file, "w") as text_file:
                text_file.write(log)
        return psnr_mean, ssim_mean


def train(args, model, train_set):
    # to cuda
    model.cuda()
    model.train()

    # dataloader
    train_loader = torch.utils.data.DataLoader(
        train_set,
        batch_size=args.batch_size,
        drop_last=True,
        shuffle=True,
        num_workers=int(args.num_threads))

    # optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.StepLR(
        optimizer, step_size=args.scheduler_step_size, gamma=args.scheduler_gamma)

    # saver
    saver = Saver(args)

    # loss function
    criterion = torch.nn.L1Loss()

    # time
    time_start_train = time.time()

    # misc
    num_batch = train_set.__len__() // args.batch_size
    counter = 0
    backup_codes(args)

    # compute paras
    params = network_paras(model)
    log = "num of parameters: {:,}".format(params)
    saver.save_log(log)
    print(log)

    # init weights
    def weights_init(m):
        if isinstance(m, torch.nn.Conv2d):
            init.kaiming_normal_(m.weight.data)

    if not args.is_finetuning:
        model.apply(weights_init)

    # start training
    print('{:=^40}'.format(' training start '))
    for epoch in range(args.epochs):
        scheduler.step(epoch)
        running_loss = 0.0
        for bidx, (_, im_lr, im_hr) in enumerate(train_loader):
            im_lr = Variable(im_lr.cuda(), volatile=False)
            im_hr = Variable(im_hr.cuda())

            # zero the parameter gradients
            model.zero_grad()

            # forward
            output = model(im_lr)

            # loss
            loss = criterion(output, im_hr)

            # backward & update
            loss.backward()
            optimizer.step()

            # accumulate running loss
            running_loss += loss.cpu().item()

            # print for every N batch
            if counter % args.step_print_loss == 0:
                # time
                acc_time = time.time() - time_start_train

                # log
                log = 'epoch: (%d/%d) [%5d/%5d], loss: %.6f | time: %s' % \
                    (epoch, args.epochs, bidx, num_batch, running_loss, str(datetime.timedelta(seconds=acc_time)))

                print(log)
                saver.save_log(log)
                running_loss = 0.0

                print_lr(optimizer)

            if counter and counter % args.step_save == 0:
                # save
                saver.save_model(model)

            # counter increment
            counter += 1

    print('{:=^40}'.format(' Finish '))
    runtime = time.time() - time_start_train
    print('training time:', str(datetime.timedelta(seconds=runtime))+'\n\n')
