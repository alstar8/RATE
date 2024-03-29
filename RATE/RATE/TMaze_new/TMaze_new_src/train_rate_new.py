import os
import datetime
import torch
import numpy as np
from tqdm import tqdm
import torch
import wandb
from torch.utils.data import random_split, DataLoader
import argparse

OMP_NUM_THREADS = '1'
os.environ['OMP_NUM_THREADS'] = OMP_NUM_THREADS 

from RATE_model.RATE import mem_transformer_v2
from TMaze_new.TMaze_new_src.tmaze_new_dataset import TMaze_data_generator, CombinedDataLoader
from TMaze_new.TMaze_new_src.val_tmaze import get_returns_TMaze
from TMaze_new.TMaze_new_src.additional import plot_cringe


seeds_list = [0,2,3,4,6,9,13,15,18,24,25,31,3,40,41,42,43,44,48,49,50,
              51,62,63,64,65,66,69,70,72,73,74,75,83,84,85,86,87,88,91,
              92,95,96,97,98,100,102,105,106,107,1,5,7,8,10,11,12,14,16,
              17,19,20,21,22,23,26,27,28,29,30,32,34,35,36,37,38,39,45,
              46,47,52,53,54,55,56,57,58,59,60,61,67,68,71,76,77,78,79,80,81,82]

def train(model, optimizer, scheduler, raw_model, segments_count, wandb_step, ckpt_path, config, train_dataloader, val_dataloader):
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
    
    wwandb = config["wwandb"]
    print(f"model parameters: {sum(p.numel() for p in list(model.parameters()))}")
    it_counter = 0
    best_val_loss = np.inf
    epochs_without_improvement = 0
    max_epochs_without_improvement = 50
    suc_rate, ep_time = 0, 0
    
    learning_rate_decay = 0.99
    patience = 100
    switch = False
    block_size = 3*config["context_length"]
    EFFECTIVE_SIZE_BLOCKS = config["context_length"] * config["sections"]
    BLOCKS_CONTEXT = block_size//3
    
    pbar = tqdm(range(config["epochs"]))
    for epoch in pbar:
        train_imgs = []
        is_train = True
        model.train()
        for it, batch in enumerate(train_dataloader):
            s, a, rtg, d, timesteps, masks = batch
            #print('1', s.shape)
            #print(s.shape)
            memory = None
            mem_tokens = None
            
            if config["model_mode"] == 'DT':
                block_part_range = range(1)
            else:
                block_part_range = range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT)
                
            for block_part in block_part_range:
                if config["model_mode"] == 'DT':
                    x1 = s.to(device)
                    y1 = a.to(device).float()
                    r1 = rtg.to(device).float()
                    t1 = timesteps.to(device)
                    masks1 = masks.to(device)
                else:
                    from_idx = block_part*(BLOCKS_CONTEXT)
                    to_idx = (block_part+1)*(BLOCKS_CONTEXT)
                    x1 = s[:, from_idx:to_idx, :].to(device)
                    y1 = a[:, from_idx:to_idx, :].to(device).float()
                    r1 = rtg[:,:,:][:, from_idx:to_idx, :].to(device).float() 
                    t1 = timesteps[:, from_idx:to_idx].to(device)
                    masks1 = masks[:, from_idx:to_idx].to(device)
                #print('2', x1.shape)
                model.flag = 1 if block_part == list(range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT))[-1] else 0

                if mem_tokens is not None:
                    mem_tokens = mem_tokens.detach()
                elif raw_model.mem_tokens is not None:
                    mem_tokens = raw_model.mem_tokens.repeat(1, r1.shape[0], 1)
                with torch.set_grad_enabled(is_train):
                    optimizer.zero_grad()
                    res = model(x1, y1, r1, y1, t1, *memory, mem_tokens=mem_tokens, masks=masks1) if memory is not None else model(x1, y1, r1, y1, t1, mem_tokens=mem_tokens, masks=masks1)
                    memory = res[0][2:]
                    logits, loss = res[0][0], res[0][1]
                    mem_tokens = res[1]
                    #train_imgs.append(model.attn_map)
                    train_loss_all = model.loss_all
                    if model.flag == 1:
                        train_loss_last = model.loss_last
                        train_loss = train_loss_all + train_loss_last * model.loss_last_coef
                    else:
                        train_loss = train_loss_all
                    if wwandb and model.flag == 1:
                        wandb.log({"full_train_loss":  train_loss.item(), "train_last_loss": train_loss_last.item(), 
                                   "train_loss": train_loss_all.item(), "train_accuracy": model.accuracy, "train_last_acc": model.last_acc})

                if is_train:
                    model.zero_grad()
                    optimizer.zero_grad()
                    train_loss.backward(retain_graph=True)
                    if config["grad_norm_clip"] is not None:
                        torch.nn.utils.clip_grad_norm_(model.parameters(), config["grad_norm_clip"])
                    optimizer.step()
                    scheduler.step()
                    lr = optimizer.state_dict()['param_groups'][0]['lr']
            it_counter += 1 
        
        # Val
        val_imgs = []
        model.eval()
        is_train = False
        with torch.no_grad():
            for it, batch in enumerate(val_dataloader):        
                s, a, rtg, d, timesteps, masks = batch
                memory = None
                mem_tokens = None
                #print('3', s.shape)
                if config["model_mode"] == 'DT':
                    block_part_range = range(1)
                else:
                    block_part_range = range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT)
                    
                for block_part in block_part_range:
                    if config["model_mode"] == 'DT':
                        x1 = s.to(device)
                        y1 = a.to(device).float()
                        r1 = rtg.to(device).float()
                        t1 = timesteps.to(device)
                        masks1 = masks.to(device)
                    else:
                        from_idx = block_part*(BLOCKS_CONTEXT)
                        to_idx = (block_part+1)*(BLOCKS_CONTEXT)
                        x1 = s[:, from_idx:to_idx, :].to(device)
                        y1 = a[:, from_idx:to_idx, :].to(device).float()
                        r1 = rtg[:,:,:][:, from_idx:to_idx, :].to(device).float() 
                        t1 = timesteps[:, from_idx:to_idx].to(device)
                        masks1 = masks[:, from_idx:to_idx].to(device)
                        
                    #print('4', x1.shape)
                    model.flag = 1 if block_part == list(range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT))[-1] else 0
                    if mem_tokens is not None:
                        mem_tokens = mem_tokens.detach()
                    elif raw_model.mem_tokens is not None:
                        mem_tokens = raw_model.mem_tokens.repeat(1, r1.shape[0], 1)
                    with torch.set_grad_enabled(is_train):
                        optimizer.zero_grad()
                        res = model(x1, y1, r1, y1, t1, *memory, mem_tokens=mem_tokens, masks=masks1) if memory is not None else model(x1, y1, r1, y1, t1, mem_tokens=mem_tokens, masks=masks1)
                        memory = res[0][2:]
                        logits, loss = res[0][0], res[0][1]
                        mem_tokens = res[1]
                        #val_imgs.append(model.attn_map)
                        val_loss_all = model.loss_all
                        if model.flag == 1:
                            val_loss_last = model.loss_last
                            val_loss = val_loss_all + val_loss_last * model.loss_last_coef
                        else:
                            val_loss = val_loss_all
                        if wwandb and model.flag == 1:
                            wandb.log({"full_val_loss":  val_loss.item(), "val_last_loss": val_loss_last.item(), 
                                       "val_loss": val_loss_all.item(), "val_accuracy": model.accuracy, "val_last_acc": model.last_acc})
                    if model.flag == 1:
                        pbar.set_description(f"ep {epoch+1} it {it} tTotal {train_loss.item():.2f} vTotal {val_loss.item():.2f} lr {lr:e} SR {suc_rate:.2f} D[T] {ep_time:.2f}")

        # Early stopping
        if val_loss_all < best_val_loss:
            best_val_loss = val_loss_all
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1

        # Проверка условия early stopping
        if epochs_without_improvement >= max_epochs_without_improvement:
            print("Early stopping!")
            break        
        
        # Scheduler changer
        if it_counter >= config["warmup_steps"] and switch == False:
            #scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=learning_rate_decay, patience=patience, mode="min")
            scheduler = torch.optim.lr_scheduler.LinearLR(optimizer, start_factor=1.0, end_factor=0.001, total_iters=config["epochs"]*config["max_segments"])
            switch = True
        
        # if wwandb:
        #     if model.flag == 1:
        #         image = np.concatenate(train_imgs[-(list(range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT))[-1]+1):], axis=1)
        #         images = wandb.Image(image, caption="Train attention map for the 0 batch and 0 head")
        #         wandb.log({"train_attention_maps": images})
        #         train_imgs = []
                
        #         image = np.concatenate(val_imgs[-(list(range(EFFECTIVE_SIZE_BLOCKS//BLOCKS_CONTEXT))[-1]+1):], axis=1)
        #         images = wandb.Image(image, caption="Val attention map for the 0 batch and 0 head")
        #         wandb.log({"val_attention_maps": images})
        #         val_imgs = []
        #     wandb.log({"segments_count": segments_count})
        
        # Inference
        if (epoch + 1) % 50 == 0 or epoch == config["epochs"] - 1:
            inference_ne_shitay = 0
            # model.eval()
            # with torch.no_grad():
            #     goods, bads = 0, 0
            #     timers = []
            #     rewards = []
            #     seeds = seeds_list
            #     pbar2 = range(len(seeds))
            #     for indx, iii in enumerate(pbar2):
            #         episode_return, act_list, t, _ , delta_t, attn_map = get_returns_TMaze(model=model, ret=1.0, seed=seeds[iii], episode_timeout=config["episode_timeout"],
            #                                                                                   corridor_length=config["corridor_length"], context_length=config["context_length"],
            #                                                                                   device=device, act_dim=config["act_dim"], config=config, create_video=False)
            #         if episode_return == 1.0:
            #             goods += 1
            #         else:
            #             bads += 1
            #         timers.append(delta_t)
            #         rewards.append(episode_return)
                    
            #         if seeds[iii] == 2:
            #             C = plot_cringe(attn_map, config["corridor_length"], config, config["mem_at_end"])
            #             if wwandb:
            #                 Cs = wandb.Image(C, caption=f"Val attention map for the seed {seeds[indx]} with L = {config['corridor_length']} and T = {config['episode_timeout']}")
            #                 wandb.log({"inference_attention_maps": Cs})
                        
            #     suc_rate = goods / (goods + bads)
            #     ep_time = np.mean(timers)

            #     if wwandb:
            #         wandb.log({"Success_rate": suc_rate, "Mean_D[time]": ep_time})
                
            model.train()
            wandb_step += 1 
            if wwandb:
                wandb.log({"checkpoint_step": wandb_step})
            #torch.save(model.state_dict(), ckpt_path + '_' + str(wandb_step) + '_KTD.pth')
            torch.save(model.state_dict(), ckpt_path + '_save' + '_KTD.pth')
            
    return model, wandb_step, optimizer, scheduler, raw_model


parser = argparse.ArgumentParser(description='Description of your program')

parser.add_argument('--model_mode', type=str, default='RATE', help='Description of model_name argument')
parser.add_argument('--max_n_final', type=int, default=3, help='Description of max_n_final argument')
parser.add_argument('--start_seg', type=int, default=1, help='Description of max_n_final argument')
parser.add_argument('--end_seg', type=int, default=10, help='Description of max_n_final argument')

args = parser.parse_args()

model_mode = args.model_mode
start_seg = args.start_seg
end_seg = args.end_seg

# RUN = 1
for RUN in range(start_seg, end_seg+1): 
    min_n_final = 1
    max_n_final = args.max_n_final
    config = {
        # data parameters
        "max_segments": max_n_final,
        "multiplier": 50,
        "hint_steps": 1,

        "episode_timeout": max_n_final*30,
        "corridor_length": max_n_final*30-2, # 58
        # ! If inference on 9 segments always
        # "episode_timeout": 9*30,
        # "corridor_length": 9*30-2, # 58
        
        "cut_dataset": False,

        "batch_size": 32, # 64
        "warmup_steps": 100, 
        "grad_norm_clip": 1.0, # 0.25 
        "wwandb": True, 
        "sections": max_n_final,                  ##################### d_head * nmt = diff params
        "context_length": 30,
        "epochs": 250, #250
        "mode": "tmaze",
        "model_mode": model_mode,
        "state_dim": 4,
        "act_dim": 1,
        "vocab_size": 10000,
        "n_layer": 8, # 6  # 8 
        "n_head": 10, # 4 # 10
        "d_model": 256, # 64                # 256
        "d_head": 128, # 32 # divider of d_model   # 128
        "d_inner": 512, # 128 # > d_model    # 512
        "dropout": 0.05, # 0.1  
        "dropatt": 0.0, # 0.0
        "MEM_LEN": 2,
        "ext_len": 1,
        "tie_weight": False,
        "num_mem_tokens": 5, # 5
        "mem_at_end": True,
        "coef": 0.0,
        "learning_rate": 1e-4, # 1e-4
        "weight_decay": 0.1,
        "curriculum": True

    }

    if config["model_mode"] == "RATE": 
        config["MEM_LEN"] = 2 ########################### 2 FOR DTXL 0
        config["mem_at_end"] = True ########################### True FOR DTXL False
    elif config["model_mode"] == "DT":
        config["MEM_LEN"] = 0 ########################### 2 FOR DTXL 0
        config["mem_at_end"] = False ########################### True FOR DTXL False
        config["num_mem_tokens"] = 0
    elif config["model_mode"] == "DTXL":
        config["MEM_LEN"] = 2
        config["mem_at_end"] = False
        config["num_mem_tokens"] = 0
    elif config["model_mode"] == "RATEM":
        config["MEM_LEN"] = 0
        config["mem_at_end"] = True

    TEXT_DESCRIPTION = "loss_all" 
    # mini_text = f"inf_on_{max_n_final}_segments"
    # ! INF ON 9 ONLY
    mini_text = f"fix_DT_curr_inf_on_{max_n_final}_segments"
    now = datetime.datetime.now()
    date_time = now.strftime("%Y-%m-%d_%H-%M-%S").replace('-', '_')
    group = f'{TEXT_DESCRIPTION}_{mini_text}_{config["model_mode"]}_min_{min_n_final}_max_{max_n_final}'
    name = f'{TEXT_DESCRIPTION}_{mini_text}_{config["model_mode"]}_min_{min_n_final}_max_{max_n_final}_RUN_{RUN}_{date_time}'
    current_dir = os.getcwd()
    current_folder = os.path.basename(current_dir)
    ckpt_path = f'../{current_folder}/TMaze_new/TMaze_new_checkpoints/fixed_DT_new_bs/{name}/'
    isExist = os.path.exists(ckpt_path)
    if not isExist:
        os.makedirs(ckpt_path)

    if config["wwandb"]:
        run = wandb.init(project="TMaze_fixed_DT", name=name, group=group, config=config, save_code=True, reinit=True) #entity="RATE"

    TMaze_data_generator(max_segments=config["max_segments"], multiplier=config["multiplier"], hint_steps=config["hint_steps"])

    wandb_step = 0
    model, optimizer, scheduler, raw_model = None, None, None, None
    prev_ep = None
    ######################################################
    if config["curriculum"] == True:
        print("MODE: CURRICULUM")
        for n_final in range(min_n_final, max_n_final+1):
            config["sections"] = n_final

            combined_dataloader = CombinedDataLoader(n_init=min_n_final, n_final=config["sections"], multiplier=config["multiplier"], 
                                                     hint_steps=config["hint_steps"], batch_size=config["batch_size"], mode="", cut_dataset=config["cut_dataset"])

            # Split dataset into train and validation sets
            full_dataset = combined_dataloader.dataset
            train_size = int(0.8 * len(full_dataset))
            val_size = len(full_dataset) - train_size
            train_dataset, val_dataset = random_split(full_dataset, [train_size, val_size])

            # Use DataLoader to load the datasets in parallel
            train_dataloader = DataLoader(train_dataset, batch_size=config["batch_size"], shuffle=True, num_workers=4)
            val_dataloader = DataLoader(val_dataset, batch_size=config["batch_size"], shuffle=True, num_workers=4)
            print(f"Number of considered segments: {n_final}, dataset length: {len(combined_dataloader.dataset)}, Train: {len(train_dataset)}, Val: {len(val_dataset)}")
            del full_dataset
            del train_dataset
            del val_dataset
            model, wandb_step, optimizer, scheduler, raw_model = train(model, optimizer, scheduler, 
                                                                       raw_model, n_final, wandb_step, ckpt_path, config,
                                                                       train_dataloader, val_dataloader)
            del train_dataloader
            del val_dataloader
            
    elif config["curriculum"] == False:
        print("MODE: CLASSIC")
        config["sections"] = max_n_final
        combined_dataloader = CombinedDataLoader(n_init=min_n_final, n_final=config["sections"], multiplier=config["multiplier"], hint_steps=config["hint_steps"], 
                                                 batch_size=config["batch_size"], mode="", cut_dataset=config["cut_dataset"], one_mixed_dataset=True)
        # Split dataset into train and validation sets
        full_dataset = combined_dataloader.dataset
        train_size = int(0.8 * len(full_dataset))
        val_size = len(full_dataset) - train_size
        train_dataset, val_dataset = random_split(full_dataset, [train_size, val_size])

        # Use DataLoader to load the datasets in parallel
        train_dataloader = DataLoader(train_dataset, batch_size=config["batch_size"], shuffle=True, num_workers=4)
        val_dataloader = DataLoader(val_dataset, batch_size=config["batch_size"], shuffle=True, num_workers=4)
        print(f"Number of considered segments: {max_n_final}, dataset length: {len(combined_dataloader.dataset)}, Train: {len(train_dataset)}, Val: {len(val_dataset)}")
        del full_dataset
        del train_dataset
        del val_dataset
        model, wandb_step, optimizer, scheduler, raw_model = train(model, optimizer, scheduler, 
                                                                   raw_model, max_n_final, wandb_step, ckpt_path, config,
                                                                   train_dataloader, val_dataloader)
        del train_dataloader
        del val_dataloader
    
    if config["wwandb"]:
        run.finish()