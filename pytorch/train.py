import argparse
import os
import time
import math
import numpy as np
import random
import sys
import shutil
import json
import string

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable

from utils import to_gpu, Corpus, batchify, train_ngram_lm, get_ppl, create_exp_dir
from models import Seq2Seq, MLP_D, MLP_G

parser = argparse.ArgumentParser(description='PyTorch ARAE for Text')
# Path Arguments
parser.add_argument('--data_path', type=str, required=True,
                    help='location of the data corpus')
parser.add_argument('--kenlm_path', type=str, default='./kenlm',
                    help='path to kenlm directory')
parser.add_argument('--save', type=str, default='example',
                    help='output directory name')

# Data Processing Arguments
parser.add_argument('--vocab_size', type=int, default=11000,
                    help='cut vocabulary down to this size '
                         '(most frequently seen words in train)')
parser.add_argument('--lowercase', action='store_true',
                    help='lowercase all text')

# Model Arguments
parser.add_argument('--emsize', type=int, default=300,
                    help='size of word embeddings')
parser.add_argument('--nhidden', type=int, default=300,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=1,
                    help='number of layers')
parser.add_argument('--noise_r', type=float, default=0.2,
                    help='stdev of noise for autoencoder (regularizer)')
parser.add_argument('--noise_anneal', type=float, default=0.995,
                    help='anneal noise_r exponentially by this'
                         'every 100 iterations')
parser.add_argument('--hidden_init', action='store_true',
                    help="initialize decoder hidden state with encoder's")
parser.add_argument('--arch_g', type=str, default='300-300',
                    help='generator architecture (MLP)')
parser.add_argument('--arch_d', type=str, default='300-300',
                    help='critic/discriminator architecture (MLP)')
parser.add_argument('--z_size', type=int, default=100,
                    help='dimension of random noise z to feed into generator')
parser.add_argument('--temp', type=float, default=1,
                    help='softmax temperature (lower --> more discrete)')
parser.add_argument('--enc_grad_norm', type=bool, default=True,
                    help='norm code gradient from critic->encoder')
parser.add_argument('--dropout', type=float, default=0.0,
                    help='dropout applied to layers (0 = no dropout)')

# Training Arguments
parser.add_argument('--epochs', type=int, default=15,
                    help='maximum number of epochs')
parser.add_argument('--min_epochs', type=int, default=6,
                    help="minimum number of epochs to train for")
parser.add_argument('--no_earlystopping', action='store_true',
                    help="won't use KenLM for early stopping")
parser.add_argument('--patience', type=int, default=5,
                    help="number of language model evaluations without ppl "
                         "improvement to wait before early stopping")
parser.add_argument('--batch_size', type=int, default=64, metavar='N',
                    help='batch size')
parser.add_argument('--niters_ae', type=int, default=1,
                    help='number of autoencoder iterations in training')
parser.add_argument('--niters_gan_d', type=int, default=5,
                    help='number of discriminator iterations in training')
parser.add_argument('--niters_gan_g', type=int, default=1,
                    help='number of generator iterations in training')
parser.add_argument('--niters_gan_ae', type=int, default=5,
                    help='number of gan-into-ae iterations in training')
parser.add_argument('--niters_gan_schedule', type=str, default='',   # TODO
                    help='epoch counts to increase number of GAN training '
                         ' iterations (increment by 1 each time)')
parser.add_argument('--lr_ae', type=float, default=1,
                    help='autoencoder learning rate')
parser.add_argument('--lr_gan_g', type=float, default=1e-04,
                    help='generator learning rate')
parser.add_argument('--lr_gan_d', type=float, default=1e-04,
                    help='critic/discriminator learning rate')
parser.add_argument('--beta1', type=float, default=0.5,
                    help='beta1 for adam. default=0.5')
parser.add_argument('--clip', type=float, default=1,
                    help='gradient clipping, max norm')
parser.add_argument('--gan_clamp', type=float, default=0.01,
                    help='WGAN clamp')
parser.add_argument('--gan_gp_lambda', type=float, default=10,
                    help='WGAN GP penalty lambda')
parser.add_argument('--grad_lambda', type=float, default=1,
                    help='WGAN into AE lambda')

# Evaluation Arguments
parser.add_argument('--sample', action='store_true',
                    help='sample when decoding for generation')
parser.add_argument('--N', type=int, default=5,
                    help='N-gram order for training n-gram language model')
parser.add_argument('--log_interval', type=int, default=200,
                    help='interval to log autoencoder training results')

# Other
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')

args = parser.parse_args()
print(vars(args))

# Set the random seed manually for reproducibility.
random.seed(args.seed) 
np.random.seed(args.seed)
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, "
              "so you should probably run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)

###############################################################################
# Load data
###############################################################################
# create corpus  # TODO to be deleted
if args.data_path.find("snli") != -1:
    args.maxlen = 15
    args.vocab_size = 11000
    args.lowercase = True
elif args.data_path.find("1Bword") != -1:
    args.maxlen = 25
    args.vocab_size = 30000
corpus = Corpus(args.data_path,
                maxlen=args.maxlen,
                vocab_size=args.vocab_size,
                lowercase=args.lowercase)


# save arguments
ntokens = len(corpus.dictionary.word2idx)
print("Vocabulary Size: {}".format(ntokens))
args.ntokens = ntokens

# exp dir
create_exp_dir(os.path.join(args.save), ['train.py', 'models.py', 'utils.py'],
        dict=corpus.dictionary.word2idx, options=args)

def logging(str, to_stdout=True):
    with open(os.path.join(args.save, 'log.txt'), 'a') as f:
        f.write(str + '\n')
    if to_stdout:
        print(str)
logging(str(vars(args)))

eval_batch_size = 10
test_data = batchify(corpus.test, eval_batch_size, shuffle=False)
train_data = batchify(corpus.train, args.batch_size, shuffle=True)

print("Loaded data!")

###############################################################################
# Build the models
###############################################################################
autoencoder = Seq2Seq(emsize=args.emsize,
                      nhidden=args.nhidden,
                      ntokens=args.ntokens,
                      nlayers=args.nlayers,
                      noise_r=args.noise_r,
                      hidden_init=args.hidden_init,
                      dropout=args.dropout,
                      gpu=args.cuda)
gan_gen = MLP_G(ninput=args.z_size, noutput=args.nhidden, layers=args.arch_g)
gan_disc = MLP_D(ninput=args.nhidden, noutput=1, layers=args.arch_d)

print(autoencoder)
print(gan_gen)
print(gan_disc)

optimizer_ae = optim.SGD(autoencoder.parameters(), lr=args.lr_ae)
optimizer_gan_g = optim.Adam(gan_gen.parameters(),
                             lr=args.lr_gan_g,
                             betas=(args.beta1, 0.999))
optimizer_gan_d = optim.Adam(gan_disc.parameters(),
                             lr=args.lr_gan_d,
                             betas=(args.beta1, 0.999))
autoencoder = autoencoder.cuda()
gan_gen = gan_gen.cuda()
gan_disc = gan_disc.cuda()

# global vars
one = torch.Tensor(1).fill_(1).cuda()
mone = one * -1

###############################################################################
# Training code
###############################################################################
def save_model():
    print("Saving models to {}".format(args.save))
    torch.save({
        "ae": autoencoder.state_dict(),
        "gan_g": gan_gen.state_dict(),
        "gan_d": gan_disc.state_dict()
        },
        os.path.join(args.save, "model.pt"))

def load_models():
    model_args = json.load(open(os.path.join(args.save, 'options.json'), 'r'))
    word2idx = json.load(open(os.path.join(args.save, 'vocab.json'), 'r'))
    idx2word = {v: k for k, v in word2idx.items()}

    print('Loading models from {}'.format(args.save))
    loaded = torch.load(os.path.join(args.save, "model.pt"))
    autoencoder.load_state_dict(loaded.get('ae'))
    gan_gen.load_state_dict(loaded.get('gan_g'))
    gan_disc.load_state_dict(loaded.get('gan_d'))
    return model_args, idx2word, autoencoder, gan_gen, gan_disc

#  TODO
def evaluate_autoencoder(data_source, epoch):
    # Turn on evaluation mode which disables dropout.
    autoencoder.eval()
    total_loss = 0
    ntokens = len(corpus.dictionary.word2idx)
    all_accuracies = 0
    bcnt = 0
    for i, batch in enumerate(data_source):
        source, target, lengths = batch
        source = to_gpu(args.cuda, Variable(source, volatile=True))
        target = to_gpu(args.cuda, Variable(target, volatile=True))

        mask = target.gt(0)
        masked_target = target.masked_select(mask)
        # examples x ntokens
        output_mask = mask.unsqueeze(1).expand(mask.size(0), ntokens)

        # output: batch x seq_len x ntokens
        output = autoencoder(source, lengths, noise=True)
        flattened_output = output.view(-1, ntokens)

        masked_output = \
            flattened_output.masked_select(output_mask).view(-1, ntokens)
        total_loss += F.cross_entropy(masked_output/args.temp, masked_target).data

        # accuracy
        max_vals, max_indices = torch.max(masked_output, 1)
        all_accuracies += \
            torch.mean(max_indices.eq(masked_target).float()).data[0]
        bcnt += 1

        aeoutf = os.path.join(args.save, "autoencoder.txt")
        with open(aeoutf, "a") as f:
            max_values, max_indices = torch.max(output, 2)
            max_indices = \
                max_indices.view(output.size(0), -1).data.cpu().numpy()
            target = target.view(output.size(0), -1).data.cpu().numpy()
            for t, idx in zip(target, max_indices):
                # real sentence
                chars = " ".join([corpus.dictionary.idx2word[x] for x in t])
                f.write(chars + '\n')
                # autoencoder output sentence
                chars = " ".join([corpus.dictionary.idx2word[x] for x in idx])
                f.write(chars + '\n'*2)

    return total_loss[0] / len(data_source), all_accuracies/bcnt


def gen_fixed_noise(noise, to_save):
    gan_gen.eval()
    autoencoder.eval()

    fake_hidden = gan_gen(noise)
    max_indices = autoencoder.generate(fake_hidden, args.maxlen, sample=args.sample)

    with open(to_save, "w") as f:
        max_indices = max_indices.data.cpu().numpy()
        for idx in max_indices:
            # generated sentence
            words = [corpus.dictionary.idx2word[x] for x in idx]
            # truncate sentences to first occurrence of <eos>
            truncated_sent = []
            for w in words:
                if w != '<eos>':
                    truncated_sent.append(w)
                else:
                    break
            chars = " ".join(truncated_sent)
            f.write(chars + '\n')


def train_lm(data_path):
    save_path = os.path.join("/tmp", ''.join(random.choice(
            string.ascii_uppercase + string.digits) for _ in range(6)))

    indices = []
    noise = to_gpu(args.cuda, Variable(torch.ones(100, args.z_size)))
    for i in range(1000):
        noise.data.normal_(0, 1)
        fake_hidden = gan_gen(noise)
        max_indices = autoencoder.generate(fake_hidden, args.maxlen, sample=args.sample)
        indices.append(max_indices.data.cpu().numpy())
    indices = np.concatenate(indices, axis=0)

    with open(save_path, "w") as f:
        # laplacian smoothing
        for word in corpus.dictionary.word2idx.keys():
            f.write(word+'\n')
        for idx in indices:
            words = [corpus.dictionary.idx2word[x] for x in idx]
            # truncate sentences to first occurrence of <eos>
            truncated_sent = []
            for w in words:
                if w != '<eos>':
                    truncated_sent.append(w)
                else:
                    break
            chars = " ".join(truncated_sent)
            f.write(chars+'\n')
    # reverse ppl
    try:
        rev_lm = train_ngram_lm(kenlm_path=args.kenlm_path,
                            data_path=save_path,
                            output_path=save_path+".arpa",
                            N=args.N)
        with open(os.path.join(args.data_path, 'test.txt'), 'r') as f:
            lines = f.readlines()
        sentences = [l.replace('\n', '') for l in lines]
        rev_ppl = get_ppl(rev_lm, sentences)
    except:
        print("reverse ppl error: it maybe the generated files aren't valid to obtain an LM")
        rev_ppl = 1e15
    '''
    # forward ppl
    for_lm = train_ngram_lm(kenlm_path=args.kenlm_path,
                        data_path=os.path.join(args.data_path, 'train.txt'),
                        output_path=save_path+".arpa",
                        N=args.N)
    with open(save_path, 'r') as f:
        lines = f.readlines()
    sentences = [l.replace('\n', '') for l in lines]
    for_ppl = get_ppl(for_lm, sentences)
    '''
    for_ppl = 0
    return rev_ppl, for_ppl


def train_ae(epoch, batch, total_loss_ae, start_time, i):
    autoencoder.train()
    optimizer_ae.zero_grad()

    source, target, lengths = batch
    source = Variable(source.cuda())
    target = Variable(target.cuda())
    output = autoencoder(source, lengths, noise=True)

    mask = target.gt(0)
    masked_target = target.masked_select(mask)
    output_mask = mask.unsqueeze(1).expand(mask.size(0), ntokens)
    flat_output = output.view(-1, ntokens)
    masked_output = flat_output.masked_select(output_mask).view(-1, ntokens)
    loss = F.cross_entropy(masked_output / args.temp, masked_target)
    loss.backward()
    torch.nn.utils.clip_grad_norm(autoencoder.parameters(), args.clip)
    optimizer_ae.step()

    total_loss_ae += loss.data[0]
    if i % args.log_interval == 0:
        probs = F.softmax(masked_output, dim=-1)
        max_vals, max_indices = torch.max(probs, 1)
        accuracy = torch.mean(max_indices.eq(masked_target).float()).data[0]
        cur_loss = total_loss_ae / args.log_interval
        elapsed = time.time() - start_time
        logging('| epoch {:3d} | {:5d}/{:5d} batches | ms/batch {:5.2f} | '
                'loss {:5.2f} | ppl {:8.2f} | acc {:8.2f}'.format(
                epoch, i, len(train_data),
                elapsed * 1000 / args.log_interval,
                cur_loss, math.exp(cur_loss), accuracy))
        total_loss_ae = 0
        start_time = time.time()
    return total_loss_ae, start_time


def train_gan_g():
    gan_gen.train()
    optimizer_gan_g.zero_grad()

    z = Variable(torch.Tensor(args.batch_size, args.z_size).normal_(0, 1).cuda())
    fake_hidden = gan_gen(z)
    errG = gan_disc(fake_hidden)
    errG.backward(one)
    optimizer_gan_g.step()

    return errG


def grad_hook(grad):
    #gan_norm = torch.norm(grad, p=2, dim=1).detach().data.mean()
    #print(gan_norm, autoencoder.grad_norm)
    return grad * args.grad_lambda


''' Steal from https://github.com/caogang/wgan-gp/blob/master/gan_cifar10.py '''
def calc_gradient_penalty(netD, real_data, fake_data):
    bsz = real_data.size(0)
    alpha = torch.rand(bsz, 1)
    alpha = alpha.expand(bsz, real_data.size(1))  # only works for 2D XXX
    alpha = alpha.cuda()
    interpolates = alpha * real_data + ((1 - alpha) * fake_data)
    interpolates = Variable(interpolates, requires_grad=True)
    disc_interpolates = netD(interpolates)

    gradients = torch.autograd.grad(outputs=disc_interpolates, inputs=interpolates,
                                    grad_outputs=torch.ones(disc_interpolates.size()).cuda(),
                                    create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradients = gradients.view(gradients.size(0), -1)

    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean() * args.gan_gp_lambda
    return gradient_penalty


def train_gan_d(batch):
   # # clamp parameters to a cube  # TODO use the WGAN-GP
   # for p in gan_disc.parameters():
   #     p.data.clamp_(-args.gan_clamp, args.gan_clamp)

    gan_disc.train()
    optimizer_gan_d.zero_grad()

    # + samples
    source, target, lengths = batch
    source = Variable(source.cuda())
    target = Variable(target.cuda())
    real_hidden = autoencoder(source, lengths, noise=False, encode_only=True)
    errD_real = gan_disc(real_hidden.detach())
    errD_real.backward(one)

    # - samples
    z = Variable(torch.Tensor(args.batch_size, args.z_size).normal_(0, 1).cuda())
    fake_hidden = gan_gen(z)
    errD_fake = gan_disc(fake_hidden.detach())
    errD_fake.backward(mone)

    # gradient penalty
    gradient_penalty = calc_gradient_penalty(gan_disc, real_hidden.data, fake_hidden.data)
    gradient_penalty.backward()

    optimizer_gan_d.step()
    return -(errD_real - errD_fake), errD_real, errD_fake


def train_gan_d_into_ae(batch):
   # # clamp parameters to a cube
   # for p in gan_disc.parameters():
   #     p.data.clamp_(-args.gan_clamp, args.gan_clamp)

    autoencoder.train()
    optimizer_ae.zero_grad()

    source, target, lengths = batch
    source = Variable(source.cuda())
    target = Variable(target.cuda())
    real_hidden = autoencoder(source, lengths, noise=False, encode_only=True)
    real_hidden.register_hook(grad_hook)
    errD_real = gan_disc(real_hidden)
    errD_real.backward(mone)
    torch.nn.utils.clip_grad_norm(autoencoder.parameters(), args.clip)

    optimizer_ae.step()
    return errD_real


def train():
    logging("Training")
    train_data = batchify(corpus.train, args.batch_size, shuffle=True)

    # gan: preparation
    if args.niters_gan_schedule != "":
        gan_schedule = [int(x) for x in args.niters_gan_schedule.split("-")]
    else:
        gan_schedule = []
    niter_gan = 1
    fixed_noise = Variable(torch.ones(args.batch_size, args.z_size).normal_(0, 1).cuda())

    best_rev_ppl = None
    impatience = 0
    for epoch in range(1, args.epochs+1):
        # update gan training schedule
        if epoch in gan_schedule:
            niter_gan += 1
            logging("GAN training loop schedule: {}".format(niter_gan))

        total_loss_ae = 0
        epoch_start_time = time.time()
        start_time = time.time()
        niter = 0
        niter_g = 1

        while niter < len(train_data):
            # train ae
            for i in range(args.niters_ae):
                if niter >= len(train_data):
                    break  # end of epoch
                total_loss_ae, start_time = train_ae(epoch, train_data[niter],
                                total_loss_ae, start_time, niter)
                niter += 1
            # train gan
            for k in range(niter_gan):
                for i in range(args.niters_gan_d):
                    errD, errD_real, errD_fake = train_gan_d(
                            train_data[random.randint(0, len(train_data)-1)])
                for i in range(args.niters_gan_ae):
                    train_gan_d_into_ae(train_data[random.randint(0, len(train_data)-1)])
                for i in range(args.niters_gan_g):
                    errG = train_gan_g()

            niter_g += 1
            if niter_g % 100 == 0:
                autoencoder.noise_anneal(args.noise_anneal)
                logging('[{}/{}][{}/{}] Loss_D: {:.8f} (Loss_D_real: {:.8f} '
                        'Loss_D_fake: {:.8f}) Loss_G: {:.8f}'.format(
                         epoch, args.epochs, niter, len(train_data),
                         errD.data[0], errD_real.data[0],
                         errD_fake.data[0], errG.data[0]))
        # eval
        test_loss, accuracy = evaluate_autoencoder(test_data, epoch)
        logging('| end of epoch {:3d} | time: {:5.2f}s | test loss {:5.2f} | '
                'test ppl {:5.2f} | acc {:3.3f}'.format(epoch,
                (time.time() - epoch_start_time), test_loss,
                math.exp(test_loss), accuracy))
        gen_fixed_noise(fixed_noise, os.path.join(args.save,
                "{:03d}_examplar_gen".format(epoch)))

        # eval with rev_ppl and for_ppl
        rev_ppl, for_ppl = train_lm(args.data_path)
        logging("Epoch {:03d}, Reverse perplexity {}".format(epoch, rev_ppl))
        logging("Epoch {:03d}, Forward perplexity {}".format(epoch, for_ppl))
        if best_rev_ppl is None or rev_ppl < best_rev_ppl:
            impatience = 0
            best_rev_ppl = rev_ppl
            logging("New saving model: epoch {:03d}.".format(epoch))
            save_model()
        else:
            if not args.no_earlystopping and epoch >= args.min_epochs:
                impatience += 1
                if impatience > args.patience:
                    logging("Ending training")
                    sys.exit()

train()
