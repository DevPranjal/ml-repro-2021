import torch
from torch import nn
import torch.backends.cudnn as cudnn
import numpy as np
import time

from params import params
from data import get_dataloaders
from teachers import get_teacher
from students import get_student
from framework import RLF_for_Resnet, hcl
from utils.misc import AverageMeter, format_time, Logger
from test import test


cudnn.deterministic = True
cudnn.benchmark = False
if params["seed"] == 0:
    params["seed"] = np.random.randint(1000)
torch.manual_seed(params["seed"])
np.random.seed(params["seed"])
torch.cuda.manual_seed(params["seed"])


teacher = get_teacher(params["teacher"])
student = get_student(params["student"])
rlf = RLF_for_Resnet(student)  # rlf => residual learning framework

weight = torch.load(params["teacher_weight_path"])
teacher.load_state_dict(weight)

train_loader, test_loader = get_dataloaders(
    params["dataset"], params["batch_size"])
if params["dataset"] == 'cifar10':
    num_classes = 10
elif params["dataset"] == 'cifar100':
    num_classes = 100


base_loss = nn.CrossEntropyLoss().cuda()
optimizer = torch.optim.SGD(
    rlf.parameters(),
    lr=params["lr"],
    momentum=0.9,
    nesterov=True,
    weight_decay=params["weight_decay"]
)

train_log_file = f"logs/{params['dataset'] + '_' + params['student'] + '_' + params['teacher']}.txt"
logger = Logger(params=params, filename=train_log_file)
best_accuracy = 0.0
best_model = rlf

start_time = time.time()

print("starting training with the following params:")
print(params)
print()

for epoch in range(params["num_epochs"]):
    loss_avg = {
        'kd_loss': AverageMeter(),
        'base_loss': AverageMeter()
    }
    correct_preds = 0.0
    total_images = 0.0

    for i, (X, y) in enumerate(train_loader):
        X, y = X.cuda(), y.cuda()
        rlf.zero_grad()

        losses = {"kd_loss": 0, "base_loss": 0}

        # getting student and teacher features
        # authors use features obtained **after** activation for the student
        # (see rlf implementation in framework.py)
        student_features, student_preds = rlf(X)
        # authors use features obtained **before** activation for the teacher
        # authors also do not use torch.no_grad() before extracting teacher features
        with torch.no_grad():
            teacher_features, teacher_preds = teacher(
                X, is_feat=True, preact=True)

        # authors start from the second teacher features
        # teacher_features = teacher_features[1:]

        # calculating review kd loss
        for sf, tf in zip(student_features, teacher_features):
            losses['kd_loss'] += hcl(student_features, teacher_features)

        # calculating cross entropy loss
        losses['base_loss'] = base_loss(student_preds, y)

        loss = losses['kd_loss'] * \
            params['kd_loss_weight'] + losses['base_loss']
        loss.backward()
        optimizer.step()

        for key in losses:
            loss_avg[key].update(losses[key])

        # calculate running average of accuracy
        student_preds = torch.max(student_preds.data, 1)[1]
        total_images += y.size(0)
        correct_preds += (student_preds == y.data).sum().item()
        train_accuracy = correct_preds / total_images

    # calculating test accuracy and storing best results
    test_accuracy = test(test_loader)
    if test_accuracy > best_acc:
        best_acc = test_accuracy
        best_model = rlf

    # decaying lr at scheduled steps
    if epoch in params['lr_decay_steps']:
        params['lr'] *= params["lr_decay_rate"]
    for param_group in optimizer.param_groups:
        param_group['lr'] = params['lr']

    # logging results
    loss_avg = {k: loss_avg[k].val for k in loss_avg}
    log_row = {
        'epoch': str(epoch),
        'train_acc': '%.2f' % (train_accuracy*100),
        'test_acc': '%.2f' % (test_accuracy*100),
        'best_acc': '%.2f' % (best_acc*100),
        'lr': '%.5f' % (lr),
        'loss': '%.5f' % (sum(loss_avg.values())),
        'kd_loss': '%.5f' % loss_avg['kd_loss'],
        'base_loss': '%.5f' % loss_avg['base_loss'],
        'time': format_time(time.time()-start_time),
        'eta': format_time((time.time()-start_time)/(epoch+1)*(params["num_epochs"]-epoch-1)),
    }
    print(log_row)
    logger.writerow(log_row)

torch.save(best_model.state_dict(), 'pretrained/' + train_log_file + '.pt')
logger.close()
