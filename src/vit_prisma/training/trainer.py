import wandb
import torch
import torch.optim as optim
import tqdm
from tqdm.auto import tqdm
from vit_prisma.training.training_utils import calculate_accuracy, calculate_loss, set_seed
from vit_prisma.utils.wandb_utils import dataclass_to_dict, update_dataclass_from_dict
from vit_prisma.training.training_dictionary import optimizer_dict, loss_function_dict
from vit_prisma.training.schedulers import WarmupThenStepLR
import os
from torch.utils.data import Dataset, DataLoader
import dataclasses




def train(
        model,
        config,
        train_dataset,
        val_dataset,
        checkpoint_path=None,
):
    # Replace config with wandb values if they exist (esp if in hyperparam sweep)
    if config.logging.use_wandb:
        wandb.init(project=config.logging.wandb_project_name)
        sweep_values = wandb.config._items # get sweep values
        update_dataclass_from_dict(config, sweep_values)
        wandb.config.update(dataclass_to_dict(config))
    
    print("Config is:", config)

    set_seed(config.training.seed)
    model.train()
    model.to(config.training.device)
    optimizer = optimizer_dict[config.training.optimizer_name](model.parameters(), lr = config.training.lr, weight_decay = config.training.weight_decay)
    loss_fn = loss_function_dict[config.training.loss_fn_name]()

    if config.training.batch_size == -1:
        batch_size_train, batch_size_test = len(train_dataset), len(val_dataset) # use full batch
    else:
        batch_size_train, batch_size_test = config.training.batch_size, config.training.batch_size


    train_loader = DataLoader(train_dataset, batch_size=batch_size_train, shuffle=True)
    test_loader = DataLoader(val_dataset, batch_size=batch_size_test, shuffle=False)

    print(f"Length of trainloader {len(train_loader)}.")
    print(f"Length of testloader {len(test_loader)}")
    steps = 0
    # if config.training.warmup_steps > 0:
    #     scheduler = optim.lr_scheduler.LambdaLR(
    #         optimizer,
    #         lr_lambda=lambda step: min(1.0, steps/ config.training.warmup_steps),)

    scheduler = WarmupThenStepLR(optimizer, warmup_steps=config.training.warmup_steps, step_size=config.training.scheduler_step, gamma=config.training.scheduler_gamma)

    if checkpoint_path and os.path.exists(checkpoint_path):
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch'] + 1
        print(f"Loaded checkpoint from epoch {checkpoint['epoch']}")

    for epoch in tqdm(range(1, config.training.num_epochs + 1)):
        for idx, items in enumerate(train_loader):
            if steps % config.logging.log_frequency == 0:
                model.eval()
                logs = {}
                train_loss = calculate_loss(model, train_loader, loss_fn, config.training.device)
                train_acc = calculate_accuracy(model, train_loader, config.training.device)
                test_loss = calculate_loss(model, test_loader, loss_fn, config.training.device)
                test_acc = calculate_accuracy(model, test_loader, config.training.device)
                tqdm.write(f"Steps{steps} | Train loss: {train_loss:.6f} | Train acc: {train_acc:.5f} | Test loss: {test_loss:.6f} | Test acc: {test_acc:.5f}")
                log_dict = {
                "train_loss": train_loss,
                "train_acc": train_acc,
                "test_loss": test_loss,
                "test_acc": test_acc,
                }
                if config.logging.use_wandb:
                    wandb.log(log_dict, step=steps)
                model.train() # set model back to train mode
            images, labels, *metadata = items
            images, labels = images.to(config.training.device), labels.to(config.training.device)
            optimizer.zero_grad()
            y = model(images)
            loss = loss_fn(y, labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.training.max_grad_norm) if config.training.max_grad_norm is not None else None
            optimizer.step()
            scheduler.step() if config.training.warmup_steps > 0 else None
            tqdm.write(f"Epoch {epoch} | steps{steps} | Loss {loss.item()}") if config.logging.print_every and steps % config.logging.print_every == 0 else None
            
            if steps % config.saving.save_cp_frequency == 0:
                torch.save({
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'epoch': epoch
                }, os.path.join(os.path.join(config.saving.parent_dir, config.saving.save_dir), f"model_{steps}.pth"))
            if hasattr(config.training, 'max_steps') and config.training.max_steps and steps >= config.training.max_steps:
                break
            
            steps += 1
    if config.logging.use_wandb:
        wandb.finish()
    return model