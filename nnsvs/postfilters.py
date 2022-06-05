import numpy as np
import torch
from nnsvs.base import BaseModel
from nnsvs.multistream import split_streams
from nnsvs.util import init_weights
from torch import nn


def variance_scaling(gv, feats, offset=2):
    """Variance scaling method to enhance synthetic speech quality

    Method proposed in :cite:t:`silen2012ways`.

    Args:
        gv (tensor): global variance computed over training data
        feats (tensor): input features
        offset (int): offset

    Returns:
        tensor: scaled features
    """
    utt_gv = feats.var(0)
    utt_mu = feats.mean(0)
    out = feats.copy()
    out[:, offset:] = (
        np.sqrt(gv[offset:] / utt_gv[offset:]) * (feats[:, offset:] - utt_mu[offset:])
        + utt_mu[offset:]
    )
    return out


class SimplifiedTADN(nn.Module):
    """Simplified temporal adaptive de-normalization for Gaussian noise

    Args:
        channels (int): number of channels
        kernel_size (int): kernel size. Default is 7.
    """

    def __init__(self, channels, kernel_size=7):
        super().__init__()
        C = channels
        padding = (kernel_size - 1) // 2
        # NOTE: process each channel independently by setting groups=C
        self.conv_gamma = nn.Conv1d(
            C, C, kernel_size=kernel_size, padding=padding, groups=C
        )

    def forward(self, z, c):
        """Forward pass

        Args:
            z (torch.Tensor): input Gaussian noise of shape (B, C, T)
            c (torch.Tensor): input 2d feature of shape (B, C, T)

        Returns:
            torch.Tensor: output 2d feature of shape (B, C, T)
        """
        # NOTE: assuming z ~ N(0, I)
        # (B, C, T)
        gamma = torch.sigmoid(self.conv_gamma(c))
        # N(0, I) -> N(0, I*gamma) where gamma is a learned parameter in [0, 1]
        return z * gamma


class Conv2dPostFilter(BaseModel):
    """A post-filter based on Conv2d

    A model proposed in :cite:t:`kaneko2017generative`.

    Args:
        channels (int): number of channels
        kernel_size (tuple): kernel sizes for Conv2d
        use_noise (bool): whether to use noise
        use_tadn (bool): whether to use temporal adaptive de-normalization
        init_type (str): type of initialization
    """

    def __init__(
        self,
        in_dim=None,
        channels=128,
        kernel_size=(5, 5),
        use_noise=True,
        use_tadn=False,
        residual=True,
        init_type="kaiming_normal",
        scale=1.0,
        noise_type="bin_wise",
        padding_mode="zeros",
    ):
        super().__init__()
        self.use_noise = use_noise
        self.noise_type = noise_type
        self.residual = residual
        self.scale = scale
        C = channels
        assert len(kernel_size) == 2
        ks = np.asarray(list(kernel_size))
        padding = (ks - 1) // 2
        self.conv1 = nn.Sequential(
            nn.Conv2d(
                2 if use_noise else 1,
                C,
                kernel_size=ks,
                padding=padding,
                padding_mode=padding_mode,
            ),
            nn.ReLU(),
        )
        self.conv2 = nn.Sequential(
            nn.Conv2d(
                C + 1, C * 2, kernel_size=ks, padding=padding, padding_mode=padding_mode
            ),
            nn.ReLU(),
        )
        self.conv3 = nn.Sequential(
            nn.Conv2d(
                C * 2 + 1, C, kernel_size=ks, padding=padding, padding_mode=padding_mode
            ),
            nn.ReLU(),
        )
        self.conv4 = nn.Conv2d(
            C + 1, 1, kernel_size=ks, padding=padding, padding_mode=padding_mode
        )

        if use_tadn:
            assert in_dim is not None, "in_dim must be provided"
            assert noise_type == "bin_wise", "tadn only works for bin_wise"
            self.tadn = SimplifiedTADN(in_dim)
        else:
            self.tadn = None

        if self.noise_type == "frame_wise":
            # noise: (B, T, 1)
            self.fc = nn.Linear(1, in_dim)
        elif self.noise_type == "bin_wise":
            # noise: (B, T, C)
            self.fc = None
        else:
            raise ValueError("Unknown noise type: {}".format(self.noise_type))

        init_weights(self, init_type)

    def forward(self, x, lengths=None):
        """Forward step

        Args:
            x (torch.Tensor): input tensor of shape (B, T, C)
            lengths (torch.Tensor): lengths of shape (B,)

        Returns:
            torch.Tensor: output tensor of shape (B, T, C)
        """
        # (B, T, C) -> (B, 1, T, C):
        x = x.unsqueeze(1)

        if self.noise_type == "bin_wise":
            z = torch.randn_like(x) * self.scale
        elif self.noise_type == "frame_wise":
            z = torch.randn(x.shape[0], 1, x.shape[2], 1).to(x.device) * self.scale
            z = self.fc(z)

        if self.use_noise:
            # adaptively scale z
            if self.tadn is not None:
                z = (
                    self.tadn(
                        z.squeeze(1).transpose(1, 2), x.squeeze(1).transpose(1, 2)
                    )
                    .transpose(1, 2)
                    .unsqueeze(1)
                )

        x_syn = x

        if self.use_noise:
            y = self.conv1(torch.cat([x_syn, z], dim=1))
        else:
            y = self.conv1(x_syn)

        y = self.conv2(torch.cat([x_syn, y], dim=1))
        y = self.conv3(torch.cat([x_syn, y], dim=1))
        residual = self.conv4(torch.cat([x_syn, y], dim=1))
        if self.residual:
            out = x_syn + residual
        else:
            out = residual

        # (B, 1, T, C) -> (B, T, C)
        out = out.squeeze(1)

        return out


class Conv1dPostFilter(nn.Module):
    """A post-filter based on 1-d convolutions

    Args:
        channels (int): number of channels
        kernel_size (int): kernel size
        use_noise (bool): whether to use noise
        use_tadn (bool): whether to use temporal adaptive de-normalization
        init_type (str): type of initialization
    """

    def __init__(
        self,
        in_dim=None,
        channels=16,
        kernel_size=5,
        use_noise=False,
        use_tadn=False,
        init_type="kaiming_normal",
        scale=1.0,
        padding_mode="zeros",
    ):
        super().__init__()
        self.use_noise = use_noise
        self.scale = scale
        C = channels
        padding = (kernel_size - 1) // 2
        self.conv1 = nn.Sequential(
            nn.Conv1d(
                2 if use_noise else 1,
                C,
                kernel_size=kernel_size,
                padding=padding,
                padding_mode=padding_mode,
            ),
            nn.ReLU(),
        )
        self.conv2 = nn.Sequential(
            nn.Conv1d(
                C + 1,
                C * 2,
                kernel_size=kernel_size,
                padding=padding,
                padding_mode=padding_mode,
            ),
            nn.ReLU(),
        )
        self.conv3 = nn.Sequential(
            nn.Conv1d(
                C * 2 + 1,
                C,
                kernel_size=kernel_size,
                padding=padding,
                padding_mode=padding_mode,
            ),
            nn.ReLU(),
        )
        self.conv4 = nn.Conv1d(
            C + 1,
            1,
            kernel_size=kernel_size,
            padding=padding,
            padding_mode=padding_mode,
        )

        if use_tadn:
            assert in_dim is not None, "in_dim must be provided"
            assert use_noise, "use_noise must be True"
            self.tadn = SimplifiedTADN(in_dim)
        else:
            self.tadn = None

        init_weights(self, init_type)

    def forward(self, x, lengths=None):
        # (B, T, C) -> (B, C, T):
        x = x.transpose(1, 2)
        x_syn = x

        if self.use_noise:
            z = torch.randn_like(x) * self.scale
            if self.tadn is not None:
                z = self.tadn(z, x)
            y = self.conv1(torch.cat([x_syn, z], dim=1))
        else:
            y = self.conv1(x_syn)
        y = self.conv2(torch.cat([x_syn, y], dim=1))
        y = self.conv3(torch.cat([x_syn, y], dim=1))
        residual = self.conv4(torch.cat([x_syn, y], dim=1))

        out = x_syn + residual

        # (B, C, T) -> (B, T, C)
        out = out.transpose(1, 2)

        return out


class MultistreamPostFilter(BaseModel):
    """A multi-stream post-filter that applies post-filtering for each feature stream

    Currently, post-filtering for MGC, BAP and log-F0 are supported.
    Note that it doesn't make much sense to apply post-filtering for other features.

    Args:
        mgc_postfilter (nn.Module): post-filter for MGC
        bap_postfilter (nn.Module): post-filter for BAP
        lf0_postfilter (nn.Module): post-filter for log-F0
        stream_sizes (list): sizes of each feature stream
        mgc_offset (int): offset for MGC. Defaults to 2.
    """

    def __init__(
        self,
        mgc_postfilter: nn.Module,
        bap_postfilter: nn.Module,
        lf0_postfilter: nn.Module,
        stream_sizes: list,
        mgc_offset: int = 2,
        bap_offset: int = 0,
    ):
        super().__init__()
        self.mgc_postfilter = mgc_postfilter
        self.bap_postfilter = bap_postfilter
        self.lf0_postfilter = lf0_postfilter
        self.stream_sizes = stream_sizes
        self.mgc_offset = mgc_offset
        self.bap_offset = bap_offset

    def forward(self, x, lengths=None):
        """Forward step

        Each feature stream is processed independently.

        Args:
            x (torch.Tensor): input tensor of shape (B, T, C)
            lengths (torch.Tensor): lengths of shape (B,)

        Returns:
            torch.Tensor: output tensor of shape (B, T, C)
        """
        streams = split_streams(x, self.stream_sizes)
        if len(streams) == 4:
            mgc, lf0, vuv, bap = streams
        elif len(streams) == 5:
            mgc, lf0, vuv, bap, vuv = streams
        elif len(streams) == 6:
            mgc, lf0, vuv, bap, vib, vib_flags = streams
        else:
            raise ValueError("Invalid number of streams")

        if self.mgc_postfilter is not None:
            if self.mgc_offset > 0:
                # keep unchanged for the 0-to-${mgc_offset}-th dim of mgc
                mgc0 = mgc[:, :, : self.mgc_offset]
                mgc_pf = self.mgc_postfilter(mgc[:, :, self.mgc_offset :], lengths)
                mgc_pf = torch.cat([mgc0, mgc_pf], dim=-1)
            else:
                mgc_pf = self.mgc_postfilter(mgc, lengths)
            mgc = mgc_pf

        if self.bap_postfilter is not None:
            if self.bap_offset > 0:
                # keep unchanged for the 0-to-${bap_offset}-th dim of bap
                bap0 = bap[:, :, : self.bap_offset]
                bap_pf = self.bap_postfilter(bap[:, :, self.bap_offset :], lengths)
                bap_pf = torch.cat([bap0, bap_pf], dim=-1)
            else:
                bap_pf = self.bap_postfilter(bap, lengths)
            bap = bap_pf

        if self.lf0_postfilter is not None:
            lf0 = self.lf0_postfilter(lf0, lengths)

        if len(streams) == 4:
            out = torch.cat([mgc, lf0, vuv, bap], dim=-1)
        elif len(streams) == 5:
            out = torch.cat([mgc, lf0, vuv, bap, vib], dim=-1)
        elif len(streams) == 6:
            out = torch.cat([mgc, lf0, vuv, bap, vib, vib_flags], dim=-1)

        return out


class _PadConv2dPostFilter(BaseModel):
    def __init__(
        self,
        in_dim=None,
        channels=128,
        kernel_size=5,
        init_type="kaiming_normal",
        padding_side="left",
    ):
        super().__init__()
        assert not isinstance(kernel_size, list)
        C = channels
        ks = kernel_size
        padding = (ks - 1) // 2
        self.padding = padding

        # Treat padding for the feature-axis carefully
        # use normal padding for the time-axis (i.e., (padding, padding))
        self.padding_side = padding_side
        if padding_side == "left":
            self.pad = nn.ReflectionPad2d((padding, 0, padding, padding))
        elif padding_side == "none":
            self.pad = nn.ReflectionPad2d((0, 0, padding, padding))
        elif padding_side == "right":
            self.pad = nn.ReflectionPad2d((0, padding, padding, padding))
        else:
            raise ValueError("Invalid padding side")

        self.conv1 = nn.Sequential(
            nn.Conv2d(2, C, kernel_size=(ks, ks)),
            nn.ReLU(),
        )
        # NOTE: for the subsequent layers, use fixed kernel_size 3 for feature-axis
        self.conv2 = nn.Sequential(
            nn.Conv2d(
                C + 1,
                C * 2,
                kernel_size=(ks, 3),
                padding=(padding, 1),
                padding_mode="reflect",
            ),
            nn.ReLU(),
        )
        self.conv3 = nn.Sequential(
            nn.Conv2d(
                C * 2 + 1,
                C,
                kernel_size=(ks, 3),
                padding=(padding, 1),
                padding_mode="reflect",
            ),
            nn.ReLU(),
        )
        self.conv4 = nn.Conv2d(
            C + 1, 1, kernel_size=(ks, 1), padding=(padding, 0), padding_mode="reflect"
        )

        self.fc = nn.Linear(1, in_dim)

        init_weights(self, init_type)

    def forward(self, x, z, lengths=None):
        # (B, T, C) -> (B, 1, T, C):
        x = x.unsqueeze(1)
        z = z.unsqueeze(1)

        z = self.fc(z)

        x_syn = x
        y = self.conv1(torch.cat([self.pad(x_syn), self.pad(z)], dim=1))

        if self.padding_side == "left":
            x_syn = x[:, :, :, : -self.padding]
        elif self.padding_side == "none":
            x_syn = x[:, :, :, self.padding : -self.padding]
        elif self.padding_side == "right":
            x_syn = x[:, :, :, self.padding :]

        y = self.conv2(torch.cat([x_syn, y], dim=1))
        y = self.conv3(torch.cat([x_syn, y], dim=1))
        residual = self.conv4(torch.cat([x_syn, y], dim=1))
        out = x_syn + residual

        # (B, 1, T, C) -> (B, T, C)
        out = out.squeeze(1)

        return out


class MultistreamConv2dPostFilter(nn.Module):
    """Conv2d-based multi-stream post-filter designed for MGC

    Divide the MGC transformation into low/mid/high dim transfomations
    with small overlaps. Overlap is determined by the kernel size.
    """

    def __init__(
        self,
        in_dim=None,
        channels=128,
        kernel_size=5,
        init_type="kaiming_normal",
        scale=1.0,
        stream_sizes=(8, 20, 30),
    ):
        super().__init__()
        assert len(stream_sizes) == 3
        self.padding = (kernel_size - 1) // 2
        self.scale = scale
        self.stream_sizes = stream_sizes

        self.low_postfilter = _PadConv2dPostFilter(
            stream_sizes[0] + self.padding,
            channels=channels,
            kernel_size=kernel_size,
            init_type=init_type,
            padding_side="left",
        )
        self.mid_postfilter = _PadConv2dPostFilter(
            stream_sizes[1] + 2 * self.padding,
            channels=channels,
            kernel_size=kernel_size,
            init_type=init_type,
            padding_side="none",
        )
        self.high_postfilter = _PadConv2dPostFilter(
            stream_sizes[2] + self.padding,
            channels=channels,
            kernel_size=kernel_size,
            init_type=init_type,
            padding_side="right",
        )

    def forward(self, x, lengths):
        assert x.shape[-1] == sum(self.stream_sizes)

        # (B, T, C)
        z = torch.randn(x.shape[0], x.shape[1], 1).to(x.device) * self.scale

        # Process three streams separately with a overlap width of padding
        out1 = self.low_postfilter(x[:, :, : self.stream_sizes[0] + self.padding], z)
        out2 = self.mid_postfilter(
            x[
                :,
                :,
                self.stream_sizes[0]
                - self.padding : sum(self.stream_sizes[:2])
                + self.padding,
            ],
            z,
        )
        out3 = self.high_postfilter(
            x[:, :, sum(self.stream_sizes[:2]) - self.padding :], z
        )

        # Merge the three outputs
        out = torch.cat([out1, out2, out3], dim=-1)

        return out