import os
import datetime
import torch
import numpy as np
from tqdm import tqdm
import torch
import torch.nn as nn
import wandb

OMP_NUM_THREADS = '1'
os.environ['OMP_NUM_THREADS'] = OMP_NUM_THREADS 

from RMDT_model.RMDT import mem_transformer_v2
from TMaze_new.TMaze_new_src.tmaze_new_dataset import TMaze_data_generator, CombinedDataLoader
from TMaze_new.TMaze_new_src.val_tmaze import get_returns_TMaze
from TMaze_new.TMaze_new_src.additional import plot_cringe



seeds_list = [0,2,3,4,6,9,13,15,18,24,25,31,3,40,41,42,43,44,48,49,50,
              51,62,63,64,65,66,69,70,72,73,74,75,83,84,85,86,87,88,91,
              92,95,96,97,98,100,102,105,106,107,
              1,5,7,8,10,11,12,14,16,
              17,19,20,21,22,23,26,27,28,29,30,32,34,35,36,37,38,39,45,
              46,47,52,53,54,55,56,57,58,59,60,61,67,68,71,76,77,78,79,80,81,82]

def train(model, optimizer, scheduler, raw_model, segments_count, wandb_step, ckpt_path, config):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # Use the config dictionary to initialize the model
    if model is None:
        model = mem_transformer_v2.MemTransformerLM(
            STATE_DIM=config["state_dim"],
            ACTION_DIM=config["act_dim"],
            n_token=config["vocab_size"],
            n_layer=config["n_layer"],
            n_head=config["n_head"],
            d_model=config["d_model"],
            d_head=config["d_head"],
            d_inner=config["d_inner"],
            dropout=config["dropout"],
            dropatt=config["dropatt"],
            mem_len=config["MEM_LEN"],
            ext_len=config["ext_len"],
            tie_weight=config["tie_weight"],
            num_mem_tokens=config["num_mem_tokens"],
            mem_at_end=config["mem_at_end"],
            mode=config["mode"],
        )

        model.loss_last_coef = config["coef"]
        torch.nn.init.xavier_uniform_(model.r_w_bias);
        torch.nn.init.xavier_uniform_(model.r_r_bias);
        wandb_step  = 0
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=config["learning_rate"],  weight_decay=config["weight_decay"], betas=(0.9, 0.95))
        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda steps: min((steps+1)/config["warmup_steps"], 1))
        raw_model = model.module if hasattr(model, "module") else model
        
    model.to(device)
    model.train()
    # optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate,  weight_decay=weight_decay, betas=(0.9, 0.95))
    # scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda steps: min((steps+1)/warmup_steps, 1))
    
    wwandb = config["wwandb"]
    suc_rate, ep_time = 0, 0
    imgs = []
    learning_rate_decay = 0.99
    patience = 100
    switch = False
    block_size = 3*config["context_length"]
    EFFECTIVE_SIZE_BLOCKS = config["context_length"] * config["sections"]
    BLOCKS_CONTEXT = block_size//3
    
    pbar = tqdm(range(config["epochs"]))
    for epoch in pbar:
        is_train = True
        model.train()
        for it, batch in enumerate(train_dataloader):
            s, a, rtg, d, timesteps, masks = batch
            memory = None
            mem_tokens=None
            for block_part in range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT):
                from_idx = block_part*(BLOCKS_CONTEXT)
                to_idx = (block_part+1)*(BLOCKS_CONTEXT)
                x1 = s[:, from_idx:to_idx, :].to(device)
                y1 = a[:, from_idx:to_idx, :].to(device).float()
                r1 = rtg[:,:,:][:, from_idx:to_idx, :].to(device).float() 
                t1 = timesteps[:, from_idx:to_idx].to(device)
                masks1 = masks[:, from_idx:to_idx].to(device)

                model.flag = 1 if block_part == list(range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT))[-1] else 0

                if mem_tokens is not None:
                    mem_tokens = mem_tokens.detach()
                elif raw_model.mem_tokens is not None:
                    mem_tokens = raw_model.mem_tokens.repeat(1, r1.shape[0], 1)

                with torch.set_grad_enabled(is_train):
                    optimizer.zero_grad()
                    if memory is not None:
                        res = model(x1, y1, r1, y1, t1, *memory, mem_tokens=mem_tokens, masks=masks1)
                    else:
                        res = model(x1, y1, r1, y1, t1, mem_tokens=mem_tokens, masks=masks1)
                    memory = res[0][2:]
                    logits, loss = res[0][0], res[0][1]
                    mem_tokens = res[1]
                    
                    imgs.append(model.attn_map)
                    
                    loss_all = model.loss_all

                    if model.flag == 1:
                        loss_last = model.loss_last
                        loss = loss_all + loss_last * model.loss_last_coef
                    else:
                        loss = loss_all
                    
                    if wwandb and model.flag == 1:
                        wandb.log({"full_train_loss":  loss.item(), 
                                   "last loss": loss_last.item(), 
                                   "train_loss": loss_all.item(),
                                   "accuracy": model.accuracy,
                                   "last_acc": model.last_acc})

                if is_train:
                    model.zero_grad()
                    optimizer.zero_grad()
                    loss.backward()
                    if config["grad_norm_clip"] is not None:
                        torch.nn.utils.clip_grad_norm_(model.parameters(), config["grad_norm_clip"])
                    optimizer.step()
                    scheduler.step()
                    lr = optimizer.state_dict()['param_groups'][0]['lr']
                    
                    if model.flag == 1:
                        pbar.set_description(f"ep {epoch+1} it {it}: Total {loss.item():.2f} last {loss_last.item():.2f} lr {lr:e} SR {suc_rate:.2f} D[T] {ep_time:.2f} acc {model.accuracy:.4f} last_acc {model.last_acc:.2f}")
                        
        if epoch >= config["warmup_steps"] and switch == False:
            #scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=learning_rate_decay, patience=patience, mode="min")
            scheduler = torch.optim.lr_scheduler.LinearLR(optimizer, start_factor=1.0, end_factor=0.001, total_iters=config["epochs"]*config["max_segments"])
            switch = True
        
        if wwandb:
            if model.flag == 1:
                image = np.concatenate(imgs[-(list(range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT))[-1]+1):], axis=1)
                images = wandb.Image(image, caption="Train attention map for the 0 batch and 0 head")
                wandb.log({"train_attention_maps": images})
                imgs = []
            wandb.log({"segments_count": segments_count})

        if (epoch + 1) % 50 == 0 or epoch == config["epochs"] - 1:
            model.eval()
            with torch.no_grad():
                goods, bads = 0, 0
                timers = []
                rewards = []
                seeds = seeds_list
                pbar2 = range(len(seeds))
                for indx, iii in enumerate(pbar2):
                    episode_return, act_list, t, _ , delta_t, attn_map = get_returns_TMaze(model=model, ret=1.0, seed=seeds[iii], episode_timeout=config["episode_timeout"],
                                                                                              corridor_length=config["corridor_length"], context_length=config["context_length"],
                                                                                              device=device, act_dim=config["act_dim"], config=config, create_video=False)
                    if episode_return == 1.0:
                        goods += 1
                    else:
                        bads += 1
                    timers.append(delta_t)
                    rewards.append(episode_return)
                    
                    if seeds[iii] == 2:
                        C = plot_cringe(attn_map, config["corridor_length"], config, config["mem_at_end"])
                        if wwandb:
                            Cs = wandb.Image(C, caption=f"Val attention map for the seed {seeds[indx]} with L = {config['corridor_length']} and T = {config['episode_timeout']}")
                            wandb.log({"val_attention_maps": Cs})
                        
                    

                suc_rate = goods / (goods + bads)
                ep_time = np.mean(timers)

                if wwandb:
                    wandb.log({"Success_rate": suc_rate, "Mean_D[time]": ep_time})
                
            model.train()
            wandb_step += 1 
            if wwandb:
                wandb.log({"checkpoint_step": wandb_step})
            torch.save(model.state_dict(), ckpt_path + '_' + str(wandb_step) + '_KTD.pth')
            
            
    return model, wandb_step, optimizer, scheduler, raw_model

RUN = 1
min_n_final = 1
max_n_final = 3
config = {
    # data parameters
    "max_segments": max_n_final,
    "multiplier": 10,
    "hint_steps": 1,
    
    "episode_timeout": 60,
    "corridor_length": 59, # 58
            
    "batch_size": 64, # 32
    "warmup_steps": 60, 
    "grad_norm_clip": 1.0, # 0.25 
    "wwandb": True, 
    "sections": min_n_final,                  #####################
    "context_length": 30,
    "epochs": 250,
    "mode": "tmaze",
    "model_mode": "RMDT",
    "state_dim": 4,
    "act_dim": 1,
    "vocab_size": 10000,
    "n_layer": 8, # 6   
    "n_head": 10, # 4
    "d_model": 256, # 64                # 256
    "d_head": 128, # 32 # divider of d_model   # 128
    "d_inner": 512, # 128 # > d_model    # 512
    "dropout": 0.05, # 0.1
    "dropatt": 0.0, # 0.0
    "MEM_LEN": 2,
    "ext_len": 1,
    "tie_weight": False,
    "num_mem_tokens": 1, # 0
    "mem_at_end": True,
    "coef": 0.0,
    "learning_rate": 1e-5,
    "weight_decay": 0.1,
    
}

if config["model_mode"] == "RMDT":
    config["MEM_LEN"] = 2 ########################### 2 FOR DTXL 0
    config["mem_at_end"] = True ########################### True FOR DTXL False
elif config["model_mode"] == "DT":
    config["MEM_LEN"] = 0 ########################### 2 FOR DTXL 0
    config["mem_at_end"] = False ########################### True FOR DTXL False
elif config["model_mode"] == "DTXL":
    config["MEM_LEN"] = 2
    config["mem_at_end"] = False
    config["num_mem_tokens"] = 0
elif config["model_mode"] == "RMDTM":
    config["MEM_LEN"] = 0
    config["mem_at_end"] = True


now = datetime.datetime.now()
date_time = now.strftime("%Y-%m-%d_%H-%M-%S").replace('-', '_')
group = f'{config["model_mode"]}_min_{min_n_final}_max_{max_n_final}'
name = f'{config["model_mode"]}_min_{min_n_final}_max_{max_n_final}_RUN_{RUN}_{date_time}'
current_dir = os.getcwd()
current_folder = os.path.basename(current_dir)
ckpt_path = f'../{current_folder}/TMaze_new/TMaze_new_checkpoints/{name}/'
isExist = os.path.exists(ckpt_path)
if not isExist:
    os.makedirs(ckpt_path)

if config["wwandb"]:
    wandb.init(project="TMaze_curriculum_v2", name=name, group=group, config=config, save_code=True, resume="allow")
    
TMaze_data_generator(max_segments=config["max_segments"], multiplier=config["multiplier"], hint_steps=config["hint_steps"])

   

wandb_step = 0
model, optimizer, scheduler, raw_model = None, None, None, None
prev_ep = None
######################################################
for n_final in range(min_n_final, max_n_final+1):
    config["sections"] = n_final
    
    combined_dataloader = CombinedDataLoader(n_init=min_n_final, n_final=config["sections"], multiplier=config["multiplier"], 
                                             hint_steps=1, batch_size=config["batch_size"], mode="", cut_dataset=False)
    train_dataloader = combined_dataloader.dataloader
    print(f"Number of considered segments: {n_final}, dataset length: {len(combined_dataloader.dataset)}")

    model, wandb_step, optimizer, scheduler, raw_model = train(model, optimizer, scheduler, raw_model, n_final, wandb_step, ckpt_path, config)