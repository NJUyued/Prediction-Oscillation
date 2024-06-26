import torch
import torch.nn.functional as F
import numpy as np
import os.path as osp
import os
from contextlib import nullcontext
from torch.cuda.amp import autocast, GradScaler
from data import FreeMatchDataManager
from networks import avail_models
import pprint
import torch.nn as nn
from utils import (
    
    FreeMatchOptimizer,   
    FreeMatchScheduler, 
    TensorBoardLogger, 
    EMA,
    SelfAdaptiveThresholdLoss,
    SelfAdaptiveFairnessLoss,
    CELoss,
    enable_running_stats,
    disable_running_stats,
)

from sklearn.metrics import (
    classification_report, 
    confusion_matrix, 
    accuracy_score, 
    precision_score, 
    recall_score,
    f1_score
)

class FreeMatchTrainer:

    def __init__(
            self,
            cfg
    ):
        
        self.cfg = cfg

        # Gathering the freematch training params.
        self.num_train_iters = cfg.TRAINER.NUM_TRAIN_ITERS
        self.num_eval_iters = cfg.TRAINER.NUM_EVAL_ITERS
        self.num_warmup_iters = cfg.TRAINER.NUM_WARMUP_ITERS
        self.num_log_iters = cfg.TRAINER.NUM_LOG_ITERS
        self.ema_val = cfg.TRAINER.EMA_VAL
        self.ulb_loss_ratio = cfg.TRAINER.ULB_LOSS_RATIO
        self.ent_loss_ratio = cfg.TRAINER.ENT_LOSS_RATIO
        self.device = 'cuda' if cfg.USE_CUDA else 'cpu'

        self.x_sharp = cfg.X_SHARP
        self.rho = cfg.RHO
        
        self.num_classes = cfg.DATASET.NUM_CLASSES
        if self.device == 'cuda':
            torch.cuda.set_device(cfg.TRAINER.GPU)
            torch.backends.cudnn.benchmark = True
            
        # Building model and setup EMA
        self.model = avail_models[cfg.MODEL.NAME](
            num_classes=cfg.DATASET.NUM_CLASSES,
            pretrained=cfg.MODEL.PRETRAINED,
            pretrained_path=cfg.MODEL.PRETRAINED_PATH
        )
        print('Number of Trainable Params: ', sum(p.numel() for p in self.model.parameters() if p.requires_grad))
        self.model = self.model.to(self.device)
        self.model.train()
        
        self.net = EMA(
            model=self.model,
            decay=self.ema_val
        )

        self.net.train()
        
        # Use Tensorboard if logging is enabled
        if cfg.USE_TB:
            self.tb = TensorBoardLogger(
                fpath=osp.join(cfg.LOG_DIR, cfg.RUN_NAME),
                filename=cfg.TB_DIR 
            )
        
        # Build available dataloaders
        self.dm = FreeMatchDataManager(cfg.DATASET, cfg.TRAINER.NUM_TRAIN_ITERS)
        self.dm.data_statistics

        # Build the optimizer and scheduler
        self.optim = FreeMatchOptimizer(self.model, cfg.OPTIMIZER)
        self.sched = FreeMatchScheduler(
            optimizer=self.optim,
            num_train_iters=self.num_train_iters,
        )

        # Initializing the loss functions
        self.sat_criterion = SelfAdaptiveThresholdLoss(cfg.TRAINER.SAT_EMA)
        self.ce_criterion = CELoss()
        self.saf_criterion = SelfAdaptiveFairnessLoss()
        
        # Initialize the class params
        self.curr_iter = 0
        self.best_test_iter = -1
        self.best_test_acc = -1
        self.p_t = torch.ones(cfg.DATASET.NUM_CLASSES) / cfg.DATASET.NUM_CLASSES
        self.label_hist = torch.ones(cfg.DATASET.NUM_CLASSES) / cfg.DATASET.NUM_CLASSES
        self.tau_t = self.p_t.mean()

        self.amp = nullcontext
        if cfg.TRAINER.AMP_ENABLED:
            self.scaler = GradScaler()
            self.amp = autocast

        # Load Model if resume is true
        if cfg.CONT_TRAIN:
            print('Loading model from the path: %s' % cfg.RESUME)
            self.__load__model__(cfg.RESUME)
            
        if self.num_warmup_iters > 0:
            print('Starting warmup training on labeled data...')
            self.warmup_train()
            print('Evaluating after warmup')
            validate_dict = self.validate()
            pprint.pprint(validate_dict, indent=4)
        
        self.__toggle__device__()

    def norm(self, tensor_list, p=2):
        """Compute p-norm for tensor list"""
        return torch.cat([x.flatten() for x in tensor_list if x is not None]).norm(p)
        
    def warmup_train(self):
        
        # Mainly of SVHN training...
        self.model.train()
        
        # for gpu profiling
        start_batch = torch.cuda.Event(enable_timing=True)
        end_batch = torch.cuda.Event(enable_timing=True)
        start_run = torch.cuda.Event(enable_timing=True)
        end_run = torch.cuda.Event(enable_timing=True)
    
        start_batch.record()
        
        for batch_lb in self.dm.train_lb_dl:
            
            if self.curr_iter >= self.num_warmup_iters:
                self.curr_iter = 0
                break
            
            end_batch.record()
            torch.cuda.synchronize()
            start_run.record()
            
            img_lb_w, label_lb = batch_lb['img_w'], batch_lb['label']
            img_lb_w, label_lb = img_lb_w.to(self.device), label_lb.to(self.device) 

            with self.amp():
                out = self.net(img_lb_w)                
                logits = out['logits']
                loss = self.ce_criterion(logits, label_lb, reduction='mean')
            
            if self.cfg.TRAINER.AMP_ENABLED:
                self.scaler.scale(loss).backward()
                self.scaler.step(self.optim.optimizer)
                self.scaler.update()
            else:
                loss.backward()
                self.optim.step()
            
            end_run.record()
            torch.cuda.synchronize()
            
            log_dict = {
                'warmup/loss': loss.item(),
                'warmup/lr': self.optim.optimizer.param_groups[0]['lr'],
                'warmup/fetch_time': start_batch.elapsed_time(end_batch) / 1000,
                'warmup/run_time': start_run.elapsed_time(end_run) / 1000
            }
            
            if (self.curr_iter + 1) % self.num_log_iters == 0:
                pprint.pprint(log_dict, indent=4)
            
            self.curr_iter += 1
            del log_dict
            start_batch.record()
    
            self.model.eval()
            probs = list()
            with torch.no_grad():
                for _, batch in enumerate(self.dm.test_dl):
                    img_lb_w, label = batch['img_w'], batch['label']
                    img_lb_w, label = img_lb_w.to(self.device), label.to(self.device)
                    out = self.model(img_lb_w)
                    logits = out['logits']
                    probs.append(logits.softmax(dim=-1))
                    
            probs = torch.cat(probs)
            max_probs, max_idx = torch.max(probs, dim=-1)

            self.tau_t = max_probs.mean()
            self.p_t = torch.mean(probs, dim=0)
            label_hist = torch.bincount(max_idx, minlength=probs.shape[1]).to(probs.dtype) 
            self.label_hist = label_hist / label_hist.sum()

    def train(self):
    
        print('Starting model training...')
        
        self.model.train()
        
        # for gpu profiling
        start_batch = torch.cuda.Event(enable_timing=True)
        end_batch = torch.cuda.Event(enable_timing=True)
        start_run = torch.cuda.Event(enable_timing=True)
        end_run = torch.cuda.Event(enable_timing=True)

        start_batch.record()

        num_samples = len(self.dm.train_ulb_dl.dataset)  # 示例数目
        pseudo_label_dim = self.num_classes  # 伪标签的维度
        label_bank_w = torch.ones(num_samples, pseudo_label_dim, device='cuda') / pseudo_label_dim
        label_bank_s = torch.ones(num_samples, pseudo_label_dim, device='cuda') / pseudo_label_dim
        for (batch_lb, batch_ulb) in zip(self.dm.train_lb_dl, self.dm.train_ulb_dl):
            
            if self.curr_iter >= self.num_train_iters:
                break

            end_batch.record()
            torch.cuda.synchronize()
            start_run.record()
            
            img_lb_w, label_lb = batch_lb['img_w'], batch_lb['label']
            img_ulb_w, img_ulb_s, idx = batch_ulb['img_w'], batch_ulb['img_s'], batch_ulb['idx']
            
            img_lb_w, label_lb = img_lb_w.to(self.device), label_lb.to(self.device) 
            img_ulb_w, img_ulb_s = img_ulb_w.to(self.device), img_ulb_s.to(self.device)
            
            num_lb = img_lb_w.shape[0]
            num_ulb = img_ulb_w.shape[0]
            
            assert num_ulb == img_ulb_s.shape[0]
            
            img = torch.cat([img_lb_w, img_ulb_w, img_ulb_s])
            with self.amp():
                if self.x_sharp:
                    enable_running_stats(self.net.model)
                out = self.net(img)    
                logits = out['logits']
                logits_lb = logits[:num_lb]
                logits_ulb_w, logits_ulb_s = logits[num_lb:].chunk(2)

                loss_lb = self.ce_criterion(logits_lb, label_lb, reduction='mean')
                
                if not self.x_sharp:
                    loss_sat, mask, self.tau_t, self.p_t, self.label_hist = self.sat_criterion(
                        logits_ulb_w, logits_ulb_s, self.tau_t, self.p_t, self.label_hist
                    )

                    loss_saf, hist_p_ulb_s = self.saf_criterion(mask, logits_ulb_s, self.p_t, self.label_hist) 
                    loss = loss_lb + self.ulb_loss_ratio * loss_sat + self.ent_loss_ratio * loss_saf

                else:
                    with torch.no_grad():
                        self.grad_w = torch.autograd.grad(loss_lb, self.net.model.parameters(), allow_unused=True)
                        scale = self.rho / self.norm(self.grad_w)
                        self.eps = [g * scale if g is not None else None for g in self.grad_w]

                        # model perturbation
                        for p, v in zip(self.net.model.parameters(), self.eps):
                            if v is not None:
                                p.add_(v)

                    # second propagation step
                    disable_running_stats(self.net.model)

                    out_hat = self.net(img)
                    logits_hat = out_hat['logits']
                    logits_lb_hat = logits_hat[:num_lb]
                    logits_ulb_w_hat, logits_ulb_s_hat = logits_hat[num_lb:].chunk(2)

                    loss_lb = self.ce_criterion(logits_lb_hat, label_lb, reduction='mean')

                    loss_sat, mask, self.tau_t, self.p_t, self.label_hist = self.sat_criterion(
                        logits_ulb_w, logits_ulb_s_hat, self.tau_t, self.p_t, self.label_hist)

                    loss_saf, hist_p_ulb_s = self.saf_criterion(mask, logits_ulb_s_hat, self.p_t, self.label_hist) 
                    ## New 
                    label_w_expanded = label_bank_w[idx]  
                    label_s_expanded = label_bank_s[idx] 
                    pseudo_label_g = torch.softmax(logits_ulb_w_hat, dim=-1)
                    pseudo_label_s = torch.softmax(logits_ulb_s_hat, dim=-1)

                    # po = torch.abs(pseudo_label_g - label_w_expanded.detach())
                    # po_s = torch.abs(pseudo_label_s - label_s_expanded.detach())
                    po = (pseudo_label_g - label_w_expanded.detach())
                    po_s = (pseudo_label_s - label_s_expanded.detach())
                    # po = nn.functional.normalize(pseudo_label_g - label_w_expanded.detach(), dim=1)
                    # po_s = nn.functional.normalize(pseudo_label_s - label_s_expanded.detach(), dim=1)
                    # x = F.normalize(pseudo_label_g - label_w_expanded.detach(), dim=-1, p=2)
                    # y = F.normalize(pseudo_label_s - label_s_expanded.detach(), dim=-1, p=2)

                    mse_loss_fn = nn.MSELoss()
                    loss_po = mse_loss_fn(po, po_s)
                    # loss_po = 2 - 2 * (x * y).sum(dim=-1)
                    # loss_po = loss_po.mean()

                    if self.cfg.TRAINER.POLOSS:
                        loss = loss_lb + self.ulb_loss_ratio * loss_sat + self.ent_loss_ratio * loss_saf + loss_po
                    else:
                        loss = loss_lb + self.ulb_loss_ratio * loss_sat + self.ent_loss_ratio * loss_saf
                 
                    label_bank_w[idx] = pseudo_label_g.detach()
                    label_bank_s[idx] = pseudo_label_s.detach()

            if self.cfg.TRAINER.AMP_ENABLED:
                self.scaler.scale(loss).backward()
                if self.x_sharp:
                    with torch.no_grad():
                        for p, v, g in zip(self.net.model.parameters(), self.eps, self.grad_w):
                            if v is not None:
                                p.sub_(v)
                                p.grad += g
                self.scaler.step(self.optim.optimizer)
                self.scaler.update()
            else:
                loss.backward()
                if self.x_sharp:
                    with torch.no_grad():
                        for p, v, g in zip(self.net.model.parameters(), self.eps, self.grad_w):
                            if v is not None:
                                p.sub_(v)
                                p.grad += g
                self.optim.step()
            
            self.sched.step()
            self.net.update()
            self.model.zero_grad()

            end_run.record()
            torch.cuda.synchronize()
            
            # Logging in tensorboard
            log_dict = {
                'train/lb_loss': loss_lb.item(),
                'train/sat_loss': loss_sat.item(),
                'train/saf_loss': loss_saf.item(),
                'train/po_loss': loss_po.item(),
                'train/total_loss': loss.item(),
                'train/mask': 1 - mask.mean().item(),
                'train/tau_t': self.tau_t.item(),
                'train/p_t': self.p_t.mean().item(),
                'train/label_hist': self.label_hist.mean().item(),
                'train/label_hist_s': hist_p_ulb_s.mean().item(),
                'train/lr': self.optim.optimizer.param_groups[0]['lr']
            } 
            
            if (self.curr_iter + 1) % self.num_eval_iters == 0:
                
                print('Evaluating...')
                validate_dict = self.validate()
                log_dict.update(validate_dict)
                save_dir = osp.join(self.cfg.LOG_DIR, self.cfg.RUN_NAME, self.cfg.OUTPUT_DIR)
                if not osp.exists(save_dir):
                    os.makedirs(save_dir)
                    
                if validate_dict['validation/accuracy'] > self.best_test_acc:
                    self.best_test_acc = validate_dict['validation/accuracy']
                    self.best_test_iter = self.curr_iter
                    self.__save__model__(save_dir, 'best_checkpoint.pth')
    
                self.__save__model__(save_dir, 'last_checkpoint.pth')
            
                log_dict.update(
                            {
                                'best_acc': self.best_test_acc,
                                'best_iter': self.best_test_iter
                            }
                )
                self.tb.update(log_dict, self.curr_iter)
                
            if (self.curr_iter + 1) % self.num_log_iters == 0:
                
                print('Iteration: %d / %d' % (self.curr_iter + 1, self.num_train_iters))
                print('Fetch Time: %.3f, Run Time: %.3f' % (start_batch.elapsed_time(end_batch) / 1000, start_run.elapsed_time(end_run) / 1000 ))
                pprint.pprint(log_dict, indent=4)

            self.curr_iter += 1
            del log_dict
            start_batch.record()

    @torch.no_grad()
    def validate(self):

        self.net.eval()
        
        total_loss, total_num = 0, 0
        labels, preds = list(), list()
        for _, batch in enumerate(self.dm.test_dl):
            
            img_lb_w, label = batch['img_w'], batch['label']
            img_lb_w, label = img_lb_w.to(self.device), label.to(self.device)
            out = self.net(img_lb_w)
            
            logits = out['logits']
            loss = self.ce_criterion(logits, label, reduction='mean')
            labels.extend(label.cpu().tolist())
            preds.extend(torch.max(logits, dim=-1)[1].cpu().tolist())
            total_num += img_lb_w.shape[0]
            total_loss += loss.detach().item() * img_lb_w.shape[0]
           
        acc = accuracy_score(labels, preds)
        precision = precision_score(labels, preds, average='macro')
        recall = recall_score(labels, preds, average='macro')
        f1 = f1_score(labels, preds, average='macro')       
        cf = confusion_matrix(labels, preds)
        cr = classification_report(labels, preds)

        print('Classification Report: \n')
        print(cr)
        
        print('Confusion Matrix \n')
        print(np.array_str(cf))

        self.net.train()
    
        return {
            'validation/loss': total_loss / total_num,
            'validation/accuracy': acc,
            'validation/precision': precision,
            'validation/recall': recall,
            'validation/f1': f1
        }

    def __save__model__(self, save_dir, save_name='latest.ckpt'):

        save_dict = {
            'model_state_dict': self.net.model.state_dict(),
            'ema_state_dict':self.net.state_dict(),
            'optimizer_state_dict': self.optim.optimizer.state_dict(),
            'scheduler_state_dict': self.sched.scheduler.state_dict(),
            'curr_iter': self.curr_iter,
            'best_test_iter': self.best_test_iter,
            'best_test_acc': self.best_test_acc,
            'tau_t': self.tau_t.cpu(),
            'p_t': self.p_t.cpu(),
            'label_hist': self.label_hist.cpu()
        }

        torch.save(save_dict, osp.join(save_dir, save_name))
        print('Model saved sucessfully. Path: %s' % osp.join(save_dir, save_name))


    def __load__model__(self, load_path):

        ckpt = torch.load(load_path)
        self.net.model.load_state_dict(ckpt['model_state_dict'])
        self.net.load_state_dict(ckpt['ema_state_dict'])
        self.optim.optimizer.load_state_dict(ckpt['optimizer_state_dict'])
        self.sched.scheduler.load_state_dict(ckpt['scheduler_state_dict'])

        # Algorithm specfic loading
        self.curr_iter = ckpt['curr_iter']
        self.tau_t = ckpt['tau_t']
        self.p_t = ckpt['p_t']
        self.label_hist = ckpt['label_hist']
        self.best_test_iter = ckpt['best_test_iter']
        self.best_test_acc = ckpt['best_test_acc']
        
        
        print('Initialized checkpoint parameters..')
        print(f'Best Accuracy: {self.best_test_acc} Best Iteration: {self.best_test_iter}')
        print('Model loaded from checkpoint. Path: %s' % load_path)

    def __toggle__device__(self):
        
        self.p_t = self.p_t.to(self.device)
        self.tau_t = self.tau_t.to(self.device)
        self.label_hist = self.label_hist.to(self.device)
