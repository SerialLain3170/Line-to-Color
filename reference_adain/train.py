import yaml
import torch
import torch.nn as nn
import argparse
import pprint

from pathlib import Path
from tqdm import tqdm
from typing import List, Dict
from torch.utils.data import DataLoader

from model import Generator, Discriminator
from dataset import IllustDataset
from visualize import Visualizer
from loss import StyleAdaINLossCalculator
from utils import session


class Trainer:
    def __init__(self,
                 config,
                 outdir,
                 modeldir,
                 data_path,
                 sketch_path,
                 ):

        self.train_config = config["train"]
        self.data_config = config["dataset"]
        model_config = config["model"]
        self.loss_config = config["loss"]

        self.outdir = outdir
        self.modeldir = modeldir

        self.dataset = IllustDataset(data_path,
                                     sketch_path,
                                     self.data_config["line_method"],
                                     self.data_config["extension"],
                                     self.data_config["train_size"],
                                     self.data_config["valid_size"],
                                     self.data_config["color_space"],
                                     self.data_config["line_space"])
        print(self.dataset)

        gen = Generator(layers=model_config["generator"]["num_layers"],
                        attn_type=model_config["generator"]["attn_type"])
        self.gen, self.gen_opt = self._setting_model_optim(gen,
                                                           model_config["generator"])

        dis = Discriminator()
        self.dis, self.dis_opt = self._setting_model_optim(dis,
                                                           model_config["discriminator"])

        self.lossfunc = StyleAdaINLossCalculator
        self.visualizer = Visualizer(self.data_config["color_space"])

    @staticmethod
    def _setting_model_optim(model: nn.Module,
                             config: Dict):
        model.cuda()
        if config["mode"] == "train":
            model.train()
        elif config["mode"] == "eval":
            model.eval()

        optimizer = torch.optim.Adam(model.parameters(),
                                     lr=config["lr"],
                                     betas=(config["b1"], config["b2"]))

        return model, optimizer

    @staticmethod
    def _valid_prepare(dataset,
                       validsize: int) -> List[torch.Tensor]:

        c_val, l_val = dataset.valid(validsize)

        return [l_val, c_val]

    @staticmethod
    def _build_dict(loss_dict: Dict[str, float],
                    epoch: int,
                    num_epochs: int) -> Dict[str, str]:

        report_dict = {}
        report_dict["epoch"] = f"{epoch}/{num_epochs}"
        for k, v in loss_dict.items():
            report_dict[k] = f"{v:.6f}"

        return report_dict

    def _eval(self,
              iteration: int,
              validsize: int,
              v_list: List[torch.Tensor]):
        torch.save(self.gen.state_dict(),
                   f"{self.modeldir}/generator_{iteration}.pt")
        torch.save(self.dis.state_dict(),
                   f"{self.modeldir}/discriminator_{iteration}.pt")

        with torch.no_grad():
            y = self.gen(v_list[0], v_list[1])

        self.visualizer(v_list, y,
                        self.outdir, iteration, validsize)

    def _iter(self, data):
        color, line = data
        color = color.cuda()
        line = line.cuda()

        loss = {}

        y = self.gen(line, color)
        dis_loss = self.loss_config["adv"] * self.lossfunc.adversarial_disloss(self.dis,
                                                                               y.detach(),
                                                                               color)

        self.dis_opt.zero_grad()
        dis_loss.backward()
        self.dis_opt.step()

        y = self.gen(line, color)
        gen_adv_loss = self.loss_config["adv"] * self.lossfunc.adversarial_genloss(self.dis,
                                                                                   y)
        content_loss = self.loss_config["content"] * self.lossfunc.content_loss(y,
                                                                                color)
        pef_loss = self.loss_config["pef"] * self.lossfunc.positive_enforcing_loss(y)

        gen_loss = gen_adv_loss + content_loss + pef_loss

        self.gen_opt.zero_grad()
        gen_loss.backward()
        self.gen_opt.step()

        loss["loss_adv_dis"] = dis_loss.item()
        loss["loss_adv_gen"] = gen_adv_loss.item()
        loss["loss_content"] = content_loss.item()
        loss["loss_pef"] = pef_loss.item()

        return loss

    def __call__(self):
        iteration = 0
        v_list = self._valid_prepare(self.dataset,
                                     self.train_config["validsize"],
                                     )

        for epoch in range(self.train_config["epoch"]):
            dataloader = DataLoader(self.dataset,
                                    batch_size=self.train_config["batchsize"],
                                    shuffle=True,
                                    drop_last=True)

            with tqdm(total=len(self.dataset)) as pbar:
                for index, data in enumerate(dataloader):
                    iteration += 1
                    loss_dict = self._iter(data)
                    report_dict = self._build_dict(loss_dict,
                                                   epoch,
                                                   self.train_config["epoch"])

                    pbar.update(self.train_config["batchsize"])
                    pbar.set_postfix(**report_dict)

                    if iteration % self.train_config["snapshot_interval"] == 1:
                        self._eval(iteration,
                                   self.train_config["validsize"],
                                   v_list,
                                   )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="StyleAdaIN")
    parser.add_argument('--session', type=str, default='adain', help="session name")
    parser.add_argument('--data_path', type=Path, help="path containing color images")
    parser.add_argument('--sketch_path', type=Path, help="path containing sketch images")
    args = parser.parse_args()

    outdir, modeldir = session(args.session)

    with open("param.yaml", "r") as f:
        config = yaml.safe_load(f)
        pprint.pprint(config)

    trainer = Trainer(config,
                      outdir,
                      modeldir,
                      args.data_path,
                      args.sketch_path)
    trainer()
