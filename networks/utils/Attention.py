from re import X
from turtle import forward
import torch.nn as nn
import torch
from timm.models.layers import to_2tuple
from typing import Optional
from networks.utils.positional_encodings import rope3, rope2, RelativePositionalBias
from networks.utils.utils import DropPath, window_partition, window_reverse, ScaleOffset, attn_norm
import torch.nn.functional as F
from torchvision import utils as vutils
from networks.utils.moe_utils import TaskMoE, router_z_loss_func, load_balancing_loss_func, Top1Router
import copy
from networks.megatron_utils.tensor_parallel.layers import ColumnParallelLinear, RowParallelLinear
from networks.megatron_utils import mpu
import networks.megatron_utils.utils

from networks.utils.positional_encodings import Rotaty2DEmbedding, Rotaty2DEmbeddingForth

class Cross_attn(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0.) -> None:
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.head_dim = dim // num_heads
        self.scale = self.head_dim ** -0.5


        
        if len(self.window_size) == 2:
            self.position_enc = rope2(self.window_size, self.head_dim)
        elif len(self.window_size) == 3:
            self.position_enc = rope3(self.window_size, self.head_dim)


        self.sr = nn.AvgPool2d(kernel_size=window_size, stride=window_size)
        self.l_q = nn.Linear(self.dim, self.dim, bias=qkv_bias)
        self.l_kv = nn.Linear(self.dim, self.dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.l_proj = nn.Linear(self.dim, self.dim)
        self.proj_drop = nn.Dropout(proj_drop)
        

        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, y):
        B, H, W, C = x.shape

        q = self.l_q(x).reshape(B, H * W, self.num_heads, self.head_dim).permute(0, 2, 1, 3)

    
        y_ = y.permute(0, 3, 1, 2)
        y_ = self.sr(y_).reshape(B, C, -1).permute(0, 2, 1)
        kv = self.l_kv(y_).reshape(B, -1, 2, self.num_heads, self.head_dim).permute(2, 0, 3, 1, 4)
        k, v = kv[0], kv[1] 

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = self.softmax(attn)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, H, W, self.dim)
        x = self.l_proj(x)
        x = self.proj_drop(x)
        return x





class Conv_attn(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0.) -> None:
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.head_dim = dim // num_heads // 4
        self.scale = self.head_dim ** -0.5

        if len(self.window_size) == 2:
            self.position_enc = rope2(self.window_size, self.head_dim)
        elif len(self.window_size) == 3:
            self.position_enc = rope3(self.window_size, self.head_dim)
        
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)
    
    def create_mask(self, x, shift_size):
        # calculate attention mask for SW-MSA
        # 保证Hp和Wp是window_size的整数倍
        # Hp = int(np.ceil(H / self.window_size[0])) * self.window_size[0]
        # Wp = int(np.ceil(W / self.window_size[1])) * self.window_size[1]
        # 拥有和feature map一样的通道排列顺序，方便后续window_partition
        
        if len(self.window_size) == 3:
            _, T, H, W, _ = x.shape
            img_mask = torch.zeros((1, T, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            t_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -shift_size[0]),
                        slice(-shift_size[0], None))
            h_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], -shift_size[1]),
                        slice(-shift_size[1], None))
            w_slices = (slice(0, -self.window_size[2]),
                        slice(-self.window_size[2], 0),
                        slice(0, None))
            cnt = 0
            for t in t_slices:
                for h in h_slices:
                    for w in w_slices:
                        img_mask[:, t, h, w, :] = cnt
                        cnt += 1
        elif len(self.window_size) == 2:
            _, H, W, _ = x.shape
            img_mask = torch.zeros((1, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            h_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -shift_size[0]),
                        slice(-shift_size[0], None))
            w_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], 0),
                        slice(0, None))
            cnt = 0
            for h in h_slices:
                for w in w_slices:
                    img_mask[:, h, w, :] = cnt
                    cnt += 1

        mask_windows = window_partition(img_mask, self.window_size)  # [nW, Mh, Mw, 1]
        if len(self.window_size) == 3:
            mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1] * self.window_size[2])  # [nW, Mh*Mw]
        elif len(self.window_size) == 2:
            mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1])  # [nW, Mh*Mw]
            
        attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)  # [nW, 1, Mh*Mw] - [nW, Mh*Mw, 1]
        # [nW, Mh*Mw, Mh*Mw]
        attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))
        return attn_mask




    def forward(self, x):
        T = 1
        if len(self.window_size) == 2:
            B, H, W, C = x.shape
        elif len(self.window_size) == 3:
            B, T, H, W, C = x.shape
        
        qkv = self.qkv(x)                    #B, H, W, C*3
        qkv_list = qkv.chunk(4, dim=-1)      #B, H, W, 3C//4
        x_all = []
        for i in range(len(qkv_list)):
            if i == 0:
                shift_size = [0, 0]
            elif i == 1:
                shift_size = [0, self.window_size[1]//2]
            elif i == 2:
                shift_size = [self.window_size[0]//2, 0]
            else:
                shift_size = [self.window_size[0]//2, self.window_size[1]//2]
            
            if shift_size[0] > 0 or shift_size[1] > 0:
                if len(self.window_size) == 3:
                    shifted_qkv = torch.roll(qkv_list[i], shifts=(-shift_size[0], -shift_size[1], -shift_size[2]), dims=(1, 2, 3))
                elif len(self.window_size) == 2:
                    shifted_qkv = torch.roll(qkv_list[i], shifts=(-shift_size[0], -shift_size[1]), dims=(1, 2))
                
                mask = self.create_mask(shifted_qkv, shift_size)
            else:
                shifted_qkv = qkv_list[i]
                mask = None
            
            N = 1
            for j in self.window_size:
                N *= j
            
            qkv_windows = window_partition(shifted_qkv, self.window_size).reshape(-1, N, 3*C//4)  # [B, nW, Mt, Mh, Mw, C]

            B_ = qkv_windows.shape[0]
            qkv_windows = qkv_windows.reshape(B_, N, 3, self.num_heads, self.head_dim).permute(2, 0, 3, 1, 4)
            q, k, v = qkv_windows.unbind(0)
            q = self.position_enc(q.reshape(-1, *self.window_size, self.head_dim)).reshape(B_, self.num_heads, -1, self.head_dim)
            k = self.position_enc(k.reshape(-1, *self.window_size, self.head_dim)).reshape(B_, self.num_heads, -1, self.head_dim)

            # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
            # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]
            q = q * self.scale
            attn = (q @ k.transpose(-2, -1))
            if mask is not None:
                # mask: [nW, Mh*Mw, Mh*Mw]
                nW = mask.shape[0]  # num_windows
                # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
                # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
                attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
                attn = attn.view(-1, self.num_heads, N, N)
                attn = self.softmax(attn)
            else:
                attn = self.softmax(attn)

            attn = self.attn_drop(attn)

            # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
            # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
            # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
            attn_windows = (attn @ v).transpose(1, 2).reshape(B_, N, C//4)

            # merge windows
            if len(self.window_size) == 3:
                attn_windows = attn_windows.view(-1, self.window_size[0], self.window_size[1], self.window_size[2], C//4)  # [nW*B, Mh, Mw, C]
            elif len(self.window_size) == 2:
                attn_windows = attn_windows.view(-1, self.window_size[0], self.window_size[1], C//4)  # [nW*B, Mh, Mw, C]
            shifted_x = window_reverse(attn_windows, self.window_size, T, H, W)  # [B, H', W', C]

            # reverse cyclic shift
            if shift_size[0] > 0 or shift_size[1] > 0:
                if len(self.window_size) == 3:
                    x = torch.roll(shifted_x, shifts=(shift_size[0], shift_size[1], shift_size[2]), dims=(1, 2, 3))
                elif len(self.window_size) == 2:
                    x = torch.roll(shifted_x, shifts=(shift_size[0], shift_size[1]), dims=(1, 2))
            else:
                x = shifted_x
            x_all.append(x)
        
        x = torch.cat(x_all, dim=-1)

        x = self.proj(x)
        x = self.proj_drop(x)
        return x
            




class Dilated_attn(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0., dilated_size=[1,1,1]) -> None:
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        
        self.dilated_size = dilated_size[-len(window_size):]
        self.window_size = window_size
        self.total_window_size = [window_size[i] * dilated_size[i] for i in range(len(window_size))]
        


        if len(self.window_size) == 2:
            self.rope_quad = rope2(self.window_size, head_dim)
        elif len(self.window_size) == 3:
            self.rope_quad = rope3(self.window_size, head_dim)
       
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)

        if len(window_size) == 2:
            self.position_enc = rope2(window_size, head_dim)
        elif len(window_size) == 3:
            self.position_enc = rope3(window_size, head_dim)
    
    def forward(self, x, mask: Optional[torch.Tensor] = None):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [batch_size, Mt, Mh, Mw, total_embed_dim]
        T=1

        if len(self.window_size) == 2:
            B, H, W, C = x.shape
        elif len(self.window_size) == 3:
            B, T, H, W, C = x.shape
        # qkv(): -> [batch_size*num_windows, Mh*Mw, 3 * total_embed_dim]
        # reshape: -> [batch_size*num_windows, Mh*Mw, 3, num_heads, embed_dim_per_head]
        # permute: -> [3, batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]

        x_windows = window_partition(x, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        x_windows = x_windows.reshape(-1, *self.total_window_size, C)
        B_ = x_windows.shape[0]
        if len(self.dilated_size) == 3:
            x_windows = window_partition(x_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], C).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1, C)
        elif len(self.dilated_size) == 2:
            x_windows = window_partition(x_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], C).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1], -1, C)
        B__, N, C = x_windows.shape


        qkv = self.qkv(x_windows).reshape(B__, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        q, k, v = qkv.unbind(0)  # make torchscript happy (cannot use tensor as tuple)

        q = self.position_enc(q.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B__, self.num_heads, -1, C // self.num_heads)
        k = self.position_enc(k.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B__, self.num_heads, -1, C // self.num_heads)

        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))


        if mask is not None:
            # mask: [nW, Mh*Mw, Mh*Mw]
            nW = mask.shape[0]  # num_windows
            # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
            # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
            attn = attn.view(B__ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        attn_windows = (attn @ v).transpose(1, 2).reshape(B__, N, C)

        if len(self.window_size) == 3:
            attn_windows = attn_windows.reshape(B_, -1, N, C).permute(0, 2, 1, 3).reshape(
                                            -1, self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], C)
            attn_windows = window_reverse(attn_windows, self.dilated_size, *self.total_window_size)
        elif len(self.window_size) == 2:
            attn_windows = attn_windows.reshape(B_, -1, N, C).permute(0, 2, 1, 3).reshape(
                                            -1, self.dilated_size[0]*self.dilated_size[1], C)
            attn_windows = window_reverse(attn_windows, self.dilated_size, 1, *self.total_window_size)
            
        attn_windows = window_reverse(attn_windows, self.total_window_size, T, H, W)

        x = self.proj(attn_windows)
        x = self.proj_drop(x)
        return x


class Swin_attn(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0., shift_size=[0,0,0]) -> None:
        super().__init__()
        self.dim = dim
        self.window_size = window_size
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.shift_size = shift_size


        if len(window_size) == 2:
            self.position_enc = rope2(window_size, head_dim)
        elif len(window_size) == 3:
            self.position_enc = rope3(window_size, head_dim)

        self.qkv = nn.Linear(dim, dim * 3, bias = qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)
    
    def create_mask(self, x):
        pass

    def forward(self, x):
        # [batch_size, Mt, Mh, Mw, total_embed_dim]
        T=1

        if len(self.window_size) == 2:
            B, H, W, C = x.shape
        elif len(self.window_size) == 3:
            B, T, H, W, C = x.shape

        if (self.shift_size[-1] == 0) or (self.window_size[-1] == W):
            mask = None
        else:
            mask = self.create_mask(x)


        if self.shift_size[-1] > 0:
            if len(self.window_size) == 3:
                shifted_x = torch.roll(x, shifts=(-self.shift_size[0], -self.shift_size[1], -self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                shifted_x = torch.roll(x, shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(1, 2))
        else:
            shifted_x=x
            mask = None



        x_windows = window_partition(shifted_x, self.window_size)  # [B, nW, Mt, Mh, Mw, C]
        if len(self.window_size) == 3:
            x = x_windows.view(-1, self.window_size[0] * self.window_size[1] * self.window_size[2], C)  # [nW*B, Mh*Mw, C]
        elif len(self.window_size) == 2:
            x = x_windows.view(-1, self.window_size[0] * self.window_size[1], C)  # [nW*B, Mh*Mw, C]
        B_, N, C = x.shape


        qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        q, k, v = qkv.unbind(0)  # make torchscript happy (cannot use tensor as tuple)

        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]

        q = self.position_enc(q.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)
        k = self.position_enc(k.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)


        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))

        # relative_position_bias_table.view: [Mh*Mw*Mh*Mw,nH] -> [Mh*Mw,Mh*Mw,nH]
        # relative_position_bias = self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
        #     self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)
        # relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # [nH, Mh*Mw, Mh*Mw]
        # attn = attn + relative_position_bias.unsqueeze(0)

        if mask is not None:
            # mask: [nW, Mh*Mw, Mh*Mw]
            nW = mask.shape[0]  # num_windows
            # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
            # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)
        # attn_save = torch.mean(attn, dim=1).to(torch.device('cpu')).reshape(-1, 32, 64)
        # for i in range(attn_save.shape[0]):
        #     save_h = i // 64
        #     save_w = i % 64
        #     vutils.save_image(attn_save[i]/attn_save[i].max(), "./img/attn%d_%d.png"%(save_h,save_w))


        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        attn_windows = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        if self.local is not True:
            attn_windows = attn_windows.reshape(B, -1, N, C).permute(0, 2, 1, 3).reshape(-1, self.window_size[0] * self.window_size[1], C)
        shifted_x = window_reverse(attn_windows, self.window_size, T, H, W).reshape(B, -1, C)


        if self.shift_size[0] > 0:
            if len(self.window_size) == 3:
                x = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1], self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                x = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1]), dims=(1, 2))
        else:
            x = shifted_x


        x = self.proj(x)
        x = self.proj_drop(x)
        return x



import numpy as np 
import numpy as np 
class SD_attn(nn.Module):
    def __init__(self, dim, window_size, num_heads, 
           qkv_bias=True, attn_drop=0., proj_drop=0., 
           shift_size=[0, 0, 0], dilated_size=[1,1,1],
           relative_position_embedding_layer=None, expand=1,
           build_from_inside=False,
           shink_input_output=False) -> None:
        super().__init__()
        if expand > 10:
            expand = expand - 10
            shink_input_output = True
        dim = dim if build_from_inside else dim*expand
        self.dim               = dim
        self.num_heads         = num_heads
        head_dim               = dim // num_heads
        self.scale             = head_dim ** -0.5
        self.dilated_size      = dilated_size[-len(window_size):]
        self.window_size       = window_size
        self.shift_size        = shift_size
        self.qkv_bias          = qkv_bias
        self.total_window_size = [window_size[i] * dilated_size[i] for i in range(len(window_size))]       
        self.attn_drop_rate    = attn_drop
        self.proj_drop_rate    = proj_drop
        self.attn_drop         = nn.Dropout(attn_drop)
        self.proj_drop         = nn.Dropout(proj_drop)
        self.softmax           = nn.Softmax(dim=-1)
        
        
        #### 2D rotaty position emebediing ##################
        assert len(window_size) == 2
        self.position_enc = rope2(window_size, head_dim, repeat = expand)
        self.relative_position_embedding_layer = None
        #### --------------------------------------------------
        
        #### ------  the only weight here ---------------------
        self.input_dim  = input_dim   = dim//expand if shink_input_output else dim
        self.inner_dim  = inner_dim   = dim
        self.output_dim = output_dim  = dim//expand if shink_input_output else dim
        self.qkv               = nn.Linear(input_dim, inner_dim * 3, bias=qkv_bias)
        self.proj              = nn.Linear(inner_dim, output_dim)
        #### --------------------------------------------------
        self.register_buffer('expand',torch.LongTensor([expand]))
        
    def grow_up_to(self,new_dim,only_inner=True):
        """
            this module is basicly doing such thing 
                -2048-[q]-128-[k]-2048-[v]-128-[p]-128-
            we just need grow up Q,K,V,P by inject two orthogonal matrix
                -2048-[q]-128-[M]-256-[Mt]-128-[k]-2048-[v]-128-[W]-256-[Wt]-128-[p]-128-
            then absorbe them into the new weight.
                -2048-[Q]-256-[K]-2048-[V]-256-[P]-128-
            -------------------------------------------------------------
            this is the inner grow_up version. 
            To extend the input version and output version, should notice:
            - to keep the same result in before-grow-up mode, the input should be 
            
        """
        
        assert new_dim%self.dim == 0
        expand = new_dim//self.dim
        new_attn_layer = SD_attn(new_dim, self.window_size, self.num_heads, 
                                 qkv_bias = self.qkv_bias, 
                                 attn_drop= self.attn_drop_rate, 
                                 proj_drop= self.proj_drop_rate, 
                                 shift_size    = self.shift_size, 
                                 dilated_size  = self.dilated_size,
                                 expand=expand,build_from_inside=True,shink_input_output=only_inner)
        print(f"growing SD_attn of {self.window_size} from ({self.input_dim}-{self.inner_dim}-{self.output_dim}) to ({new_attn_layer.input_dim}-{new_attn_layer.inner_dim}-{new_attn_layer.output_dim})")
        new_attn_layer.scale  = self.scale
        old_state_dict = self.state_dict()
        new_state_dict = {}
        
       
        ### ===> we firstly duel with the orthogonal matrix between v and p <===
        """
        notice we split `num_heads` head 
        
        at last layer: x = layer.proj(x)
        its equal to x = torch.einsum("kj,bwhj->bwhk",layer.proj.weight, x ) +   layer.proj.bias
        by insert the orthogonal matrix, we get 
        == equal => x = torch.einsum("ij,ki,bwhj->bwhk",dense_w1, layer.proj.weight, x ) +   layer.proj.bias
                                   #(768,1536) (768,768) (B,W,H,1536) -> (B,W,H,768)
        ===> x = torch.einsum("kj,bwhj->bwhk",                                  # (768,1536),(B,W,H,1536)-> (B,W,H,768)
                         torch.einsum("ij,ki->kj", dense_w1,layer.proj.weight), # (768,1536) (768,768)-> (768,1536)
                         x ) +                                                  
                         layer.proj.bias           # notice the bias do not need change
        """
        device = old_state_dict['proj.bias'].device
        w1 = []
        for i in range(self.num_heads):
            w = torch.empty(self.dim//self.num_heads, new_dim//self.num_heads)
            torch.nn.init.orthogonal_(w)
            w1.append(w)
        w1 = torch.stack(w1) #(6,128,256)
        dense_w1 = torch.diag_embed(w1.permute(1,2,0)).permute(2,0,3,1)
        dense_w1 = dense_w1.reshape(self.dim, new_dim).to(device)
        
        proj_weight = torch.einsum("ij,ki->kj", dense_w1, old_state_dict['proj.weight'])
        proj_bias   = old_state_dict['proj.bias']
        if not only_inner:
            proj_weight = torch.cat([proj_weight,proj_weight])# (768_output,1536_inner)-> (1536_output,1536_inner)
            proj_bias   = torch.cat([proj_bias  ,proj_bias  ])    # (768_output)->(1536_output)
        # (768_input, 1536_inner), (768_output,768_input) -> (768_output,1536_inner)
        new_state_dict['proj.weight']  = proj_weight
        new_state_dict['proj.bias']  = proj_bias
        
        ### ===> we secondly duel with the orthogonal matrix between q and k <===
        """
        notice we have a rotaty position embedding, which equally matmul a block-rotation matrix after get q and v
        ==> q_r = ( W_q*x + bias)@R
        ==> k_r = ( W_k*x + bias)@R
        we draw conclution here, for detail drivation, see document.
        Conclution:
            when activate this postion embediing, we must use duplicate \theta list and the orthogonal matrix should be also block-rotation.
        """
        ## lets constract the orthogonal matrix
        w2 = []
        for i in range(self.num_heads):
            matrix_blocks = []
            for _ in range(expand):matrix_blocks.append(self.position_enc.create_random_transformer_matrix())#[(128,256)]
            #for _ in range(expand):matrix_blocks.append(torch.eye(self.dim//self.num_heads))#[(128,256)]
            matrix_blocks = torch.cat(matrix_blocks,-1)/np.sqrt(expand) ## (128, 128x2) 
            w2.append(matrix_blocks)
        w2 = torch.stack(w2) #(6,128,256)
        dense_w2 = torch.diag_embed(w2.permute(1,2,0)).permute(2,0,3,1)
        dense_w2 = dense_w2.reshape(self.dim,new_dim).to(device)#(6*128,6*256)

        #print(dense_w2.shape)
        #### TODO: use matrix matmul is a very low-efficient method since the w2 is a quite sparse matrix.
        q_weight, k_weight, v_weight= old_state_dict['qkv.weight'].split([self.dim,self.dim,self.dim])
        # transfer q , use dense_w2
        q_weight= torch.einsum("jk,ji->ki", dense_w2, q_weight) # (768_inner, 1536_inner), (768_inner,768_input) -> (1536_inner,768_input)
        q_weight= torch.cat([q_weight,q_weight],-1)/2 if not only_inner else q_weight # (1536_inner,768_input) -> (1536_inner,1536_input)
        # transfer k, use dense_w2
        k_weight= torch.einsum("jk,ji->ki", dense_w2, k_weight) # (768_inner, 1536_inner), (768_inner,768_input) -> (1536_inner,768_input)
        k_weight= torch.cat([k_weight,k_weight],-1)/2 if not only_inner else k_weight # (1536_inner,768_input) -> (1536_inner,1536_input)
        # transfer v, use dense_w1
        v_weight= torch.einsum("jk,ji->ki", dense_w1, v_weight) # (768_inner, 1536_inner), (768_inner,768_input) -> (1536_inner,768_input)
        v_weight= torch.cat([v_weight,v_weight],-1)/2 if not only_inner else v_weight # (1536_inner,768_input) -> (1536_inner,1536_input)
        
        new_state_dict['qkv.weight'] = torch.cat([q_weight, k_weight, v_weight])
        
        if "qkv.bias" in old_state_dict:
            q_bias  , k_bias  ,v_bias   =   old_state_dict['qkv.bias'].split([self.dim,self.dim,self.dim])
            # transfer q , use dense_w2
            q_bias  = torch.einsum("jk,j ->k",  dense_w2, q_bias)   # (768_inner, 1536_inner), (768_inner) -> (1536_inner) 
            # transfer k, use dense_w2
            k_bias  = torch.einsum("jk,j ->k",  dense_w2, k_bias)   # (768_inner, 1536_inner), (768_inner) -> (1536_inner) 
            # transfer v, use dense_w1
            v_bias  = torch.einsum("jk,j ->k",  dense_w1, v_bias)   # (768_inner, 1536_inner), (768_inner) -> (1536_inner) 
            new_state_dict['qkv.bias']   = torch.cat([q_bias  , k_bias  , v_bias  ])
        
        
        new_state_dict["expand"]  = new_state_dict["position_enc.repeat"]  = torch.LongTensor([expand])
        for key in new_state_dict.keys():new_state_dict[key] = new_state_dict[key].to(device)
        new_attn_layer.load_state_dict(new_state_dict)
        
        return new_attn_layer
    
    def grow_up_outside(self,new_dim):
        expand = self.expand.item()
        new_attn_layer = SD_attn(new_dim, self.window_size, self.num_heads, 
                                 qkv_bias = self.qkv_bias, 
                                 attn_drop= self.attn_drop_rate, 
                                 proj_drop= self.proj_drop_rate, 
                                 shift_size    = self.shift_size, 
                                 dilated_size  = self.dilated_size,
                                 expand=expand,build_from_inside=True)
        new_attn_layer.scale  = self.scale
        old_state_dict = self.state_dict()
        new_state_dict = {}
        device = old_state_dict['proj.bias'].device
        q_weight, k_weight, v_weight= old_state_dict['qkv.weight'].split([self.dim,self.dim,self.dim])
        q_weight= torch.cat([q_weight,q_weight],-1)/2 # (1536_inner,768_input) -> (1536_inner,1536_input)
        k_weight= torch.cat([k_weight,k_weight],-1)/2 # (1536_inner,768_input) -> (1536_inner,1536_input)
        v_weight= torch.cat([v_weight,v_weight],-1)/2 # (1536_inner,768_input) -> (1536_inner,1536_input)
        new_state_dict['qkv.weight']  = torch.cat([q_weight, k_weight, v_weight])# (1536_inner *3, 1536_input)
        new_state_dict['qkv.bias']    = old_state_dict['qkv.bias']               # (1536_inner)
        new_state_dict['proj.weight'] = torch.cat([old_state_dict['proj.weight'],old_state_dict['proj.weight']],0)# (768_output,1536_inner)-> (1536_output,1536_inner)
        new_state_dict['proj.bias']   = torch.cat([old_state_dict['proj.bias'],old_state_dict['proj.bias']])# (768_output)->(1536_output)
        new_state_dict["expand"]  = new_state_dict["position_enc.repeat"]  = torch.LongTensor([expand])
        for key in new_state_dict.keys():new_state_dict[key] = new_state_dict[key].to(device)
        new_attn_layer.load_state_dict(new_state_dict)
        return new_attn_layer

    def create_mask(self, x):
        # calculate attention mask for SW-MSA
        # 保证Hp和Wp是window_size的整数倍
        # Hp = int(np.ceil(H / self.window_size[0])) * self.window_size[0]
        # Wp = int(np.ceil(W / self.window_size[1])) * self.window_size[1]
        # 拥有和feature map一样的通道排列顺序，方便后续window_partition


        if len(self.window_size) == 3:
            _, T, H, W, _ = x.shape
            img_mask = torch.zeros((1, T, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            t_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            h_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], -self.shift_size[1]),
                        slice(-self.shift_size[1], None))
            w_slices = (slice(0, -self.window_size[2]),
                        slice(-self.window_size[2], 0),
                        slice(0, None))
            cnt = 0
            for t in t_slices:
                for h in h_slices:
                    for w in w_slices:
                        img_mask[:, t, h, w, :] = cnt
                        cnt += 1
        elif len(self.window_size) == 2:
            _, H, W, _ = x.shape
            img_mask = torch.zeros((1, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            h_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            w_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], 0),
                        slice(0, None))
            cnt = 0
            for h in h_slices:
                for w in w_slices:
                    img_mask[:, h, w, :] = cnt
                    cnt += 1

        mask_windows = window_partition(img_mask, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        mask_windows = mask_windows.reshape(-1, *self.total_window_size, 1)
        B_ = mask_windows.shape[0]
        if len(self.dilated_size) == 3:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], 1).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1)
        elif len(self.dilated_size) == 2:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], 1).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1], -1)


        # mask_windows = window_partition(img_mask, self.window_size)  # [nW, Mh, Mw, 1]
        # if len(self.window_size) == 3:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1] * self.window_size[2])  # [nW, Mh*Mw]
        # elif len(self.window_size) == 2:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1])  # [nW, Mh*Mw]
            
        attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)  # [nW, 1, Mh*Mw] - [nW, Mh*Mw, 1]
        # [nW, Mh*Mw, Mh*Mw]
        attn_mask = attn_mask.masked_fill(attn_mask != 0, -torch.inf).masked_fill(attn_mask == 0, float(0.0))
        return attn_mask   
    
    def forward(self, x):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [batch_size, Mt, Mh, Mw, total_embed_dim]
        T=1

        if len(self.window_size) == 2:
            _, H, W, C = x.shape
        elif len(self.window_size) == 3:
            _, T, H, W, C = x.shape

        if (self.shift_size[-1] == 0) or (self.total_window_size[-1] == W):
            mask = None
        else:
            mask = self.create_mask(x)

        if self.shift_size[-1] > 0:
            if len(self.window_size) == 3:
                shifted_x = torch.roll(x, shifts=(-self.shift_size[0], -self.shift_size[1], -self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                shifted_x = torch.roll(x, shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(1, 2))
        else:
            shifted_x=x
            mask = None

        # qkv(): -> [batch_size*num_windows, Mh*Mw, 3 * total_embed_dim]
        # reshape: -> [batch_size*num_windows, Mh*Mw, 3, num_heads, embed_dim_per_head]
        # permute: -> [3, batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]

        x_windows = window_partition(shifted_x, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        x_windows = x_windows.reshape(-1, *self.total_window_size, C)
        B = x_windows.shape[0]
        if len(self.dilated_size) == 3:
            x_windows = window_partition(x_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], C).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1, C)
        elif len(self.dilated_size) == 2:
            x_windows = window_partition(x_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], C).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1], -1, C)
        B_, N, _ = x_windows.shape
        qkv = self.qkv(x_windows)
        qkv = qkv.reshape(B_, N, 3, self.num_heads, self.inner_dim // self.num_heads).permute(2, 0, 3, 1, 4)
        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        q, k, v = qkv.unbind(0)  # make torchscript happy (cannot use tensor as tuple)
        q = self.position_enc(q.reshape(-1, *self.window_size, self.inner_dim // self.num_heads)).reshape(B_, self.num_heads, -1, self.inner_dim // self.num_heads) # B,head,L,D
        k = self.position_enc(k.reshape(-1, *self.window_size, self.inner_dim // self.num_heads)).reshape(B_, self.num_heads, -1, self.inner_dim // self.num_heads) # B,head,L,D
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))


        if mask is not None:
            # mask: [nW, Mh*Mw, Mh*Mw]
            nW = mask.shape[0]  # num_windows
            # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
            # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        attn_windows = (attn @ v).transpose(1, 2).reshape(B_, N, self.inner_dim)

        if len(self.window_size) == 3:
            attn_windows = attn_windows.reshape(B, -1, N, self.inner_dim).permute(0, 2, 1, 3).reshape(-1, self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], self.inner_dim)
            attn_windows = window_reverse(attn_windows, self.dilated_size, *self.total_window_size)
        elif len(self.window_size) == 2:
            attn_windows = attn_windows.reshape(B, -1, N, self.inner_dim).permute(0, 2, 1, 3).reshape(-1, self.dilated_size[0]*self.dilated_size[1], self.inner_dim)
            attn_windows = window_reverse(attn_windows, self.dilated_size, 1, *self.total_window_size)
            
        shifted_x = window_reverse(attn_windows, self.total_window_size, T, H, W)

        if self.shift_size[0] > 0:
            if len(self.window_size) == 3:
                x = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1], self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                x = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1]), dims=(1, 2))
        else:
            x = shifted_x

        x = self.proj(x)
        x = self.proj_drop(x)
        return x
    
class SD_attn_Cross(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0., shift_size=[0, 0, 0], dilated_size=[1,1,1]) -> None:
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        
        self.dilated_size = dilated_size[-len(window_size):]
        self.window_size = window_size
        self.shift_size = shift_size
        self.total_window_size = [window_size[i] * dilated_size[i] for i in range(len(window_size))]
        


        if len(self.window_size) == 2:
            self.rope_quad = rope2(self.window_size, head_dim)
        elif len(self.window_size) == 3:
            self.rope_quad = rope3(self.window_size, head_dim)
       
        self.qv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.k  = nn.Linear(dim, dim * 1, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)

        if len(window_size) == 2:
            self.position_enc = rope2(window_size, head_dim)
        elif len(window_size) == 3:
            self.position_enc = rope3(window_size, head_dim)
    
    def create_mask(self, x):
            # calculate attention mask for SW-MSA
            # 保证Hp和Wp是window_size的整数倍
            # Hp = int(np.ceil(H / self.window_size[0])) * self.window_size[0]
            # Wp = int(np.ceil(W / self.window_size[1])) * self.window_size[1]
            # 拥有和feature map一样的通道排列顺序，方便后续window_partition

        if len(self.window_size) == 3:
            _, T, H, W, _ = x.shape
            img_mask = torch.zeros(
                (1, T, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            t_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            h_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], -self.shift_size[1]),
                        slice(-self.shift_size[1], None))
            w_slices = (slice(0, -self.window_size[2]),
                        slice(-self.window_size[2], 0),
                        slice(0, None))
            cnt = 0
            for t in t_slices:
                for h in h_slices:
                    for w in w_slices:
                        img_mask[:, t, h, w, :] = cnt
                        cnt += 1
        elif len(self.window_size) == 2:
            _, H, W, _ = x.shape
            img_mask = torch.zeros(
                (1, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            h_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            w_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], 0),
                        slice(0, None))
            cnt = 0
            for h in h_slices:
                for w in w_slices:
                    img_mask[:, h, w, :] = cnt
                    cnt += 1

        mask_windows = window_partition(
            img_mask, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        mask_windows = mask_windows.reshape(-1, *self.total_window_size, 1)
        B_ = mask_windows.shape[0]
        if len(self.dilated_size) == 3:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1,
                                                                                     self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], 1).permute(
                0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1)
        elif len(self.dilated_size) == 2:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1,
                                                                                     self.dilated_size[0]*self.dilated_size[1], 1).permute(
                0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1], -1)

        # mask_windows = window_partition(img_mask, self.window_size)  # [nW, Mh, Mw, 1]
        # if len(self.window_size) == 3:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1] * self.window_size[2])  # [nW, Mh*Mw]
        # elif len(self.window_size) == 2:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1])  # [nW, Mh*Mw]

        # [nW, 1, Mh*Mw] - [nW, Mh*Mw, 1]
        attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
        # [nW, Mh*Mw, Mh*Mw]
        attn_mask = attn_mask.masked_fill(
            attn_mask != 0, -torch.inf).masked_fill(attn_mask == 0, float(0.0))
        return attn_mask

    def create_windows(self,x):
        if len(self.window_size) == 2:
            _, H, W, C = x.shape
        elif len(self.window_size) == 3:
            _, T, H, W, C = x.shape

        

        if self.shift_size[-1] > 0:
            if len(self.window_size) == 3:
                shifted_x = torch.roll(
                    x, shifts=(-self.shift_size[0], -self.shift_size[1], -self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                shifted_x = torch.roll(
                    x, shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(1, 2))
        else:
            shifted_x = x
            mask = None

        # qkv(): -> [2*batch_size*num_windows, Mh*Mw, 3 * total_embed_dim]
        # reshape: -> [2*batch_size*num_windows, Mh*Mw, 3, num_heads, embed_dim_per_head]
        # permute: -> [3, 2*batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]

        x_windows = window_partition(
            shifted_x, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        x_windows = x_windows.reshape(-1, *self.total_window_size, C)
        B = x_windows.shape[0]

        dodilate = False
        if len(self.dilated_size) == 3:
            x_windows = window_partition(x_windows, self.dilated_size).reshape(B, -1, self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], C
                                                                               ).permute(0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1, C)
            dodilate = True
        elif len(self.dilated_size) == 2:
            x_windows = window_partition(x_windows, self.dilated_size).reshape(B, -1, self.dilated_size[0]*self.dilated_size[1], C
                                                                               ).permute(0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1], -1, C)
            dodilate = True
        return x_windows
    
    def forward(self, x, cross):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            cross: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [batch_size, Mt, Mh, Mw, total_embed_dim]

        T = 1
        B = x.size(0)
        if len(self.window_size) == 2:
            _, H, W, C = x.shape
        elif len(self.window_size) == 3:
            _, T, H, W, C = x.shape
        if (self.shift_size[-1] == 0) or (self.total_window_size[-1] == W):
            mask = None
        else:
            mask = self.create_mask(x)
        c_windows = self.create_windows(cross)
        x_windows = self.create_windows(x)

        assert len(c_windows) == len(x_windows)
        
        B_, N, C = x_windows.shape
        qv = self.qv(x_windows).reshape(B_, N, 2, self.num_heads,C // self.num_heads).permute(2, 0, 3, 1, 4)
        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # make torchscript happy (cannot use tensor as tuple)
        q, v = qv.unbind(0)
        k    = self.k(c_windows).reshape(B_, N, 1, self.num_heads,C // self.num_heads).permute(2, 0, 3, 1, 4)

        q = self.position_enc(q.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)
        k = self.position_enc(k.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)

        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))

        if mask is not None:
            # mask: [nW, Mh*Mw, Mh*Mw]
            nW = mask.shape[0]  # num_windows
            # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
            # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
            attn = attn.view(B_ // nW, nW, self.num_heads, N,
                             N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        attn_windows = (attn @ v).transpose(1, 2).reshape(B_, N, C)

        if len(self.window_size) == 3:
            attn_windows = attn_windows.reshape(B, -1, N, C).permute(0, 2, 1, 3).reshape(
                -1, self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], C)
            attn_windows = window_reverse(
                attn_windows, self.dilated_size, *self.total_window_size)
        elif len(self.window_size) == 2:
            attn_windows = attn_windows.reshape(B, -1, N, C).permute(0, 2, 1, 3).reshape(
                -1, self.dilated_size[0]*self.dilated_size[1], C)
            attn_windows = window_reverse(
                attn_windows, self.dilated_size, 1, *self.total_window_size)

        shifted_x = window_reverse(
            attn_windows, self.total_window_size, T, H, W)

        if self.shift_size[0] > 0:
            if len(self.window_size) == 3:
                x = torch.roll(shifted_x, shifts=(
                    self.shift_size[0], self.shift_size[1], self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                x = torch.roll(shifted_x, shifts=(
                    self.shift_size[0], self.shift_size[1]), dims=(1, 2))
        else:
            x = shifted_x

        x = self.proj(x)
        x = self.proj_drop(x)
        return x



class Flash_attn(nn.Module):
    def __init__(self, dim, window_size, uv_bias=True, attn_drop=0., proj_drop=0., expansion_factor=2, attn_type='lin') -> None:
        super().__init__()
        self.attn_type = attn_type
        self.dim = dim
        self.window_size = window_size
        self.hidden_dim = expansion_factor * dim
        self.s = 128

        seq_len = 1
        for i in window_size:
            seq_len *= i

        self.scale = 1. / seq_len

        self.uv = nn.Linear(dim, 2*self.hidden_dim+self.s, bias=uv_bias)
        self.quad_q_scaleoffset = ScaleOffset(self.s)
        self.quad_k_scaleoffset = ScaleOffset(self.s)
        self.quad_attn_drop = nn.Dropout(attn_drop)

        if self.attn_type == "lin":
            self.lin_q_scaleoffset = ScaleOffset(self.s)
            self.lin_k_scaleoffset = ScaleOffset(self.s)
            self.rope_lin = rope2((32, 64), self.s)


        self.proj = nn.Linear(self.hidden_dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        # self.softmax = nn.Softmax(dim=-1)
        self.attn_norm = attn_norm(dim=-1, method='squared_relu')

        
        self.rel_postion_bias = RelativePositionalBias(window_size, 1)
        # self.rope_quad = PositionalEncoding3D(self.s)
        if len(self.window_size) == 2:
            self.rope_quad = rope2(self.window_size, self.s)
        elif len(self.window_size) == 3:
            self.rope_quad = rope3(self.window_size, self.s)
    

        # nn.init.normal_(self.relative_position_bias_table, std=.02)
    
    def forward(self, x, mask: Optional[torch.Tensor] = None):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [batch_size*num_windows, Mh*Mw, total_embed_dim]
        T=1

        if len(self.window_size) == 2:
            B, H, W, C = x.shape
        elif len(self.window_size) == 3:
            B, T, H, W, C = x.shape
        

        x_windows = window_partition(x, self.window_size)  # [B, nW, Mt, Mh, Mw, C]
        if len(self.window_size) == 3:
            x = x_windows.view(B, -1, self.window_size[0] * self.window_size[1] * self.window_size[2], C)  # [B, nW, Mt*Mh*Mw, C]
        elif len(self.window_size) == 2:
            x = x_windows.view(B, -1, self.window_size[0] * self.window_size[1], C)  # [B, nW, Mt*Mh*Mw, C]

        B, nW, N, C = x.shape 
        x = x.view(-1, N, C)
        B_ = x.shape[0]
        # u,v:[batch_size, num_windows, Mh*Mw, hidden_dim], base:[batch_size, num_windows, Mh*Mw, s]
        u, v, base = torch.split(F.silu(self.uv(x)), [self.hidden_dim, self.hidden_dim, self.s], dim=-1)
        # quad_q, quad_k: [batch_size, num_windows, Mh*Mw, s]
        quad_q, quad_k = self.quad_q_scaleoffset(base), self.quad_k_scaleoffset(base)

        quad_q = self.rope_quad(quad_q.reshape(-1, *self.window_size, self.s)).reshape(B_, N, self.s)
        quad_k = self.rope_quad(quad_k.reshape(-1, *self.window_size, self.s)).reshape(B_, N, self.s)

        if self.attn_type == 'lin':
            lin_q, lin_k = self.lin_q_scaleoffset(base), self.lin_k_scaleoffset(base)
            lin_q = window_reverse(lin_q, self.window_size, T, H, W)
            lin_q = self.rope_lin(lin_q)
            lin_q = window_partition(lin_q, self.window_size).reshape(B_, N, self.s)
            lin_k = window_reverse(lin_k, self.window_size, T, H, W)
            lin_k = self.rope_lin(lin_k)
            lin_k = window_partition(lin_k, self.window_size).reshape(B_, N, self.s)
            
            # lin_q = lin_q / lin_q.norm(dim=-1, keepdim=True)
            # lin_k = lin_k / lin_k.norm(dim=-1, keepdim=True)
        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]

        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # quad_attn: [batch_size, num_windows, Mh*Mw, Mh*Mw]
        quad_q = quad_q * self.scale
        quad_attn = quad_q @ quad_k.transpose(-2, -1)
        quad_attn = self.rel_postion_bias(quad_attn)

        if mask is not None:
            # mask: [B, nW, Mh*Mw]
            B, nW, _ = mask.shape  # num_windows
            # attn.view: [batch_size, num_windows, Mh*Mw, Mh*Mw]
            quad_attn_mask = mask.view(B, nW, 1, -1)
            attn_mask = torch.zeros_like(quad_attn_mask, dtype=quad_q.dtype)
            attn_mask = attn_mask.masked_fill(quad_attn_mask, float("-inf"))
            quad_attn = quad_attn + attn_mask
            quad_attn = self.attn_norm(quad_attn)
        else:
            quad_attn = self.attn_norm(quad_attn)
        # quad_attn:[batch_size, num_windows, Mh*Mw, Mh*Mw]
        quad_attn = self.quad_attn_drop(quad_attn)

        # quadratic: [batch_size, num_windows, Mh*Mw, hidden_dim]
        quadratic = quad_attn @ v
        # if self.train:
        #     if self.quad_attn_scale is None:
        #         self.quad_attn_scale = 0.2 / (quadratic.abs().mean().detach()+1e-7)
        #     else:
        #         self.quad_attn_scale = self.quad_attn_scale * self.beta + (1-self.beta) * (0.2 / (quadratic.abs().mean().detach() + 1e-7))
        # quadratic = quadratic * self.quad_attn_scale

        if self.attn_type == 'lin':
            if mask is not None:
                # lin_mask: [B, nW, Mh*Mw, 1]
                lin_mask = torch.logical_not(mask).unsqueeze(-1)
                # lin_v: [B, nW, Mh*Mw, hidden_dim]
                lin_v = lin_mask * v / (N * nW * self.s)
            else:
                lin_v = v / (N * nW)
            
            # lin_kv: [B, nW, s, hidden_dim]
            lin_kv = lin_k.transpose(-2, -1) @ lin_v
            # linear: [B, nW, Mh*Mw, hidden_dim]
            linear = lin_q @ torch.sum(lin_kv, dim=-3, keepdim=True)

        # @: multiply -> [batch_size*num_windows, Mh*Mw, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        if self.attn_type == 'lin':
            x = u * (quadratic + linear)
        else:
            x = u * quadratic
        x = self.proj(x)
        x = self.proj_drop(x)

        # merge windows
        if len(self.window_size) == 3:
            attn_windows = x.view(-1, self.window_size[0], self.window_size[1], self.window_size[2], C)  # [B, nW, Mt, Mh, Mw, C]
        elif len(self.window_size) == 2:
            attn_windows = x.view(-1, self.window_size[0], self.window_size[1], C)  # [B, nW, Mh, Mw, C]
        x = window_reverse(attn_windows, self.window_size, T, H, W)  # [B, T, H, W, C]

        return x

class Hydra_attn(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0., expansion_factor=1, local=True, use_attn=True) -> None:
        super().__init__()
        self.dim = dim
        self.window_size = window_size
        self.hidden_dim = expansion_factor * dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        self.use_attn = use_attn
        self.local = local
        # self.scale = 1
        
        if self.use_attn:
            self.qkv = nn.Linear(dim, 3*self.dim, bias=qkv_bias)
        else:
            self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, 2*dim, bias=qkv_bias)

        # if self.attn_type == "lin":
        #     self.lin_q_scaleoffset = ScaleOffset(self.s)
        #     self.lin_k_scaleoffset = ScaleOffset(self.s)
        #     # self.rope_lin = PositionalEncoding3D(dim)
        #     self.rope_lin = rope3((16, 32, 64), self.s)


        self.attn_drop = nn.Dropout(attn_drop)

        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.softmax = nn.Softmax(dim=-1)
        # self.attn_norm = attn_norm(dim=-1, method='squared_relu')

        

        # self.rope_quad = PositionalEncoding3D(self.s)
        if len(window_size) == 2:
            self.position_enc = rope2(window_size, head_dim)
        elif len(window_size) == 3:
            self.position_enc = rope3(window_size, head_dim)

    
    def forward(self, x, mask: Optional[torch.Tensor] = None):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [B, T, H, W, total_embed_dim]
        T=1

        if len(self.window_size) == 2:
            B, H, W, C = x.shape
        elif len(self.window_size) == 3:
            B, T, H, W, C = x.shape
        
        hy_k, hy_v = torch.split(self.kv(x), [self.dim, self.dim], dim=-1)

        # hy_k = hy_k.view(B, -1, C)
        # hy_v = hy_v.view(B, -1, C)

        # hy_k = hy_k / hy_k.norm(dim=-2, keepdim=True)
        # hy_v = hy_v / hy_v.norm(dim=-2, keepdim=True)
        hy_k = hy_k / hy_k.norm(dim=-1, keepdim=True)
        hy_kv = (hy_k * hy_v).reshape(B, -1, C)
        hy_kv = hy_kv.sum(dim=-2, keepdim=True)

        if self.use_attn:

            x_windows = window_partition(x, self.window_size)  # [B, nW, Mt, Mh, Mw, C]
            if self.local:
                if len(self.window_size) == 3:
                    x = x_windows.view(-1, self.window_size[0] * self.window_size[1] * self.window_size[2], C)  # [nW*B, Mh*Mw, C]
                elif len(self.window_size) == 2:
                    x = x_windows.view(-1, self.window_size[0] * self.window_size[1], C)  # [nW*B, Mh*Mw, C]
            else:
                x = x_windows.view(B, -1, self.window_size[0] * self.window_size[1], C).permute(0, 2, 1, 3).reshape(B*self.window_size[0] * self.window_size[1], -1, C)
            B_, N, C = x.shape


            qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
            q, k, v = qkv.unbind(0)  # make torchscript happy (cannot use tensor as tuple)

            # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
            # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]

            q = self.position_enc(q.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)
            k = self.position_enc(k.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)


            q = q * self.scale
            attn = (q @ k.transpose(-2, -1))

            # relative_position_bias_table.view: [Mh*Mw*Mh*Mw,nH] -> [Mh*Mw,Mh*Mw,nH]
            # relative_position_bias = self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
            #     self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)
            # relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # [nH, Mh*Mw, Mh*Mw]
            # attn = attn + relative_position_bias.unsqueeze(0)

            if mask is not None:
                # mask: [nW, Mh*Mw, Mh*Mw]
                nW = mask.shape[0]  # num_windows
                # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
                # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
                attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
                attn = attn.view(-1, self.num_heads, N, N)
                attn = self.softmax(attn)
            else:
                attn = self.softmax(attn)

            attn = self.attn_drop(attn)
            # attn_save = torch.mean(attn, dim=1).to(torch.device('cpu')).reshape(-1, 32, 64)
            # for i in range(attn_save.shape[0]):
            #     save_h = i // 64
            #     save_w = i % 64
            #     vutils.save_image(attn_save[i]/attn_save[i].max(), "./img/attn%d_%d.png"%(save_h,save_w))


            # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
            # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
            # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
            attn_windows = (attn @ v).transpose(1, 2).reshape(B_, N, C)
            if self.local is not True:
                attn_windows = attn_windows.reshape(B, -1, N, C).permute(0, 2, 1, 3).reshape(-1, self.window_size[0] * self.window_size[1], C)
            attn_x = window_reverse(attn_windows, self.window_size, T, H, W).reshape(B, -1, C)

            
        else:
            hy_q = self.q(x).reshape(B, -1, C)
            attn_x = hy_q / hy_q.norm(dim=-1, keepdim=True)
        # x = attn_x * hy_kv
        if self.use_attn:
            x = attn_x
        else:
            x = attn_x * hy_kv

        x = self.proj(x)
        x = self.proj_drop(x).reshape(B, T, H, W, C).squeeze(1)


        return x


class WindowAttention(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0.) -> None:
        super().__init__()
        self.dim = dim
        self.window_size = window_size
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        
        assert len(window_size) == 3

        self.position_enc = rope3(window_size, head_dim)
       
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)
    
    def forward(self, x, mask: Optional[torch.Tensor] = None):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [batch_size*num_windows, Mh*Mw, total_embed_dim]
        B_, N, C = x.shape
        # qkv(): -> [batch_size*num_windows, Mh*Mw, 3 * total_embed_dim]
        # reshape: -> [batch_size*num_windows, Mh*Mw, 3, num_heads, embed_dim_per_head]
        # permute: -> [3, batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        q, k, v = qkv.unbind(0)  # make torchscript happy (cannot use tensor as tuple)

        q = self.position_enc(q.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)
        k = self.position_enc(k.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)

        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))


        if mask is not None:
            # mask: [nW, Mh*Mw, Mh*Mw]
            nW = mask.shape[0]  # num_windows
            # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
            # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class HiLo(nn.Module):
    """
    HiLo Attention
    """
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., window_size=2, alpha=0.5):
        super().__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."
        head_dim = int(dim/num_heads)
        self.dim = dim

        # self-attention heads in Lo-Fi
        self.l_heads = int(num_heads * alpha)
        # token dimension in Lo-Fi
        self.l_dim = self.l_heads * head_dim

        # self-attention heads in Hi-Fi
        self.h_heads = num_heads - self.l_heads
        # token dimension in Hi-Fi
        self.h_dim = self.h_heads * head_dim

        # local window size. The `s` in our paper.
        self.ws = window_size

        if (self.ws[0] == 1) and (self.ws[1] == 1):
            # ws == 1 is equal to a standard multi-head self-attention
            self.h_heads = 0
            self.h_dim = 0
            self.l_heads = num_heads
            self.l_dim = dim

        self.scale = qk_scale or head_dim ** -0.5

        # Low frequence attention (Lo-Fi)
        if self.l_heads > 0:
            if (self.ws[0] != 1) or (self.ws[1] != 1):
                self.sr = nn.AvgPool2d(kernel_size=window_size, stride=window_size)
            self.l_q = nn.Linear(self.dim, self.l_dim, bias=qkv_bias)
            self.l_kv = nn.Linear(self.dim, self.l_dim * 2, bias=qkv_bias)
            self.l_proj = nn.Linear(self.l_dim, self.l_dim)

        # High frequence attention (Hi-Fi)
        if self.h_heads > 0:
            self.h_qkv = nn.Linear(self.dim, self.h_dim * 3, bias=qkv_bias)
            self.h_proj = nn.Linear(self.h_dim, self.h_dim)

        # self.position_enc = rope2(window_size, head_dim)

    def hifi(self, x):
        B, H, W, C = x.shape
        h_group, w_group = H // self.ws[0], W // self.ws[1]

        total_groups = h_group * w_group

        x = x.reshape(B, h_group, self.ws[0], w_group, self.ws[1], C).transpose(2, 3)

        qkv = self.h_qkv(x).reshape(B, total_groups, -1, 3, self.h_heads, self.h_dim // self.h_heads).permute(3, 0, 1, 4, 2, 5)
        q, k, v = qkv[0], qkv[1], qkv[2]  # B, hw, n_head, ws*ws, head_dim
        # q = self.position_enc(q.reshape(-1, self.ws[0], self.ws[1], self.h_dim // self.h_heads)).reshape(*q.shape)
        # k = self.position_enc(k.reshape(-1, self.ws[0], self.ws[1], self.h_dim // self.h_heads)).reshape(*k.shape)

        attn = (q @ k.transpose(-2, -1)) * self.scale  # B, hw, n_head, ws*ws, ws*ws
        attn = attn.softmax(dim=-1)
        attn = (attn @ v).transpose(2, 3).reshape(B, h_group, w_group, self.ws[0], self.ws[1], self.h_dim)
        x = attn.transpose(2, 3).reshape(B, h_group * self.ws[0], w_group * self.ws[1], self.h_dim)

        x = self.h_proj(x)
        return x

    def lofi(self, x):
        B, H, W, C = x.shape

        q = self.l_q(x).reshape(B, H * W, self.l_heads, self.l_dim // self.l_heads).permute(0, 2, 1, 3)

        if self.ws[0] > 1 or self.ws[1] > 1:
            x_ = x.permute(0, 3, 1, 2)
            x_ = self.sr(x_).reshape(B, C, -1).permute(0, 2, 1)
            kv = self.l_kv(x_).reshape(B, -1, 2, self.l_heads, self.l_dim // self.l_heads).permute(2, 0, 3, 1, 4)
        else:
            kv = self.l_kv(x).reshape(B, -1, 2, self.l_heads, self.l_dim // self.l_heads).permute(2, 0, 3, 1, 4)
        k, v = kv[0], kv[1] 

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)

        x = (attn @ v).transpose(1, 2).reshape(B, H, W, self.l_dim)
        x = self.l_proj(x)
        return x

    def forward(self, x):
        B, H, W, C = x.shape

        if self.h_heads == 0:
            x = self.lofi(x)
            return x

        if self.l_heads == 0:
            x = self.hifi(x)
            return x

        hifi_out = self.hifi(x)
        lofi_out = self.lofi(x)

        x = torch.cat((hifi_out, lofi_out), dim=-1)
        return x



class SD_attn_withmoe(nn.Module):
    def __init__(self, dim, attr_len, attr_hidden_size, window_size, num_heads, qkv_bias=True, attn_drop=0., proj_drop=0., shift_size=[0, 0, 0], dilated_size=[1,1,1],
                num_experts=1, expert_capacity=1., router_bias=True, router_noise=1e-2, is_scale_prob=True, drop_tokens=True) -> None:
        super().__init__()
        self.dim = dim
        self.attr_len = attr_len
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        self.expert_capacity = expert_capacity
        self.is_scale_prob = is_scale_prob
        self.drop_tokens = drop_tokens
        self.n_experts = num_experts
        
        self.dilated_size = dilated_size[-len(window_size):]
        self.window_size = window_size
        self.shift_size = shift_size
        self.total_window_size = [window_size[i] * dilated_size[i] for i in range(len(window_size))]
        


        if len(self.window_size) == 2:
            self.rope_quad = rope2(self.window_size, head_dim)
        elif len(self.window_size) == 3:
            self.rope_quad = rope3(self.window_size, head_dim)
       
        qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)

        self.gate = Top1Router(attr_len, 
                            attr_hidden_size, 
                            num_experts, 
                            router_bias=router_bias, 
                            router_noise=router_noise)
        self.qkv = torch.nn.ModuleList(
            [copy.deepcopy(qkv) for i in range(num_experts)])
        

        self.attn_drop = nn.Dropout(attn_drop)

        proj = nn.Linear(dim, dim)

        self.proj = torch.nn.ModuleList(
            [copy.deepcopy(proj) for i in range(num_experts)])

        self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)

        if len(window_size) == 2:
            self.position_enc = rope2(window_size, head_dim)
        elif len(window_size) == 3:
            self.position_enc = rope3(window_size, head_dim)


    def create_mask(self, x):
        # calculate attention mask for SW-MSA
        # 保证Hp和Wp是window_size的整数倍
        # Hp = int(np.ceil(H / self.window_size[0])) * self.window_size[0]
        # Wp = int(np.ceil(W / self.window_size[1])) * self.window_size[1]
        # 拥有和feature map一样的通道排列顺序，方便后续window_partition


        if len(self.window_size) == 3:
            _, T, H, W, _ = x.shape
            img_mask = torch.zeros((1, T, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            t_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            h_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], -self.shift_size[1]),
                        slice(-self.shift_size[1], None))
            w_slices = (slice(0, -self.window_size[2]),
                        slice(-self.window_size[2], 0),
                        slice(0, None))
            cnt = 0
            for t in t_slices:
                for h in h_slices:
                    for w in w_slices:
                        img_mask[:, t, h, w, :] = cnt
                        cnt += 1
        elif len(self.window_size) == 2:
            _, H, W, _ = x.shape
            img_mask = torch.zeros((1, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            h_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            w_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], 0),
                        slice(0, None))
            cnt = 0
            for h in h_slices:
                for w in w_slices:
                    img_mask[:, h, w, :] = cnt
                    cnt += 1

        mask_windows = window_partition(img_mask, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        mask_windows = mask_windows.reshape(-1, *self.total_window_size, 1)
        B_ = mask_windows.shape[0]
        if len(self.dilated_size) == 3:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], 1).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1)
        elif len(self.dilated_size) == 2:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], 1).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1], -1)


        # mask_windows = window_partition(img_mask, self.window_size)  # [nW, Mh, Mw, 1]
        # if len(self.window_size) == 3:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1] * self.window_size[2])  # [nW, Mh*Mw]
        # elif len(self.window_size) == 2:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1])  # [nW, Mh*Mw]
            
        attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)  # [nW, 1, Mh*Mw] - [nW, Mh*Mw, 1]
        # [nW, Mh*Mw, Mh*Mw]
        attn_mask = attn_mask.masked_fill(attn_mask != 0, -torch.inf).masked_fill(attn_mask == 0, float(0.0))
        return attn_mask

    
    def forward(self, x, attr=None):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [batch_size, Mt, Mh, Mw, total_embed_dim]
        T=1

        if len(self.window_size) == 2:
            Bs, H, W, C = x.shape
        elif len(self.window_size) == 3:
            Bs, T, H, W, C = x.shape

        if (self.shift_size[-1] == 0) or (self.total_window_size[-1] == W):
            mask = None
        else:
            mask = self.create_mask(x)

        if self.attr_len > self.dim and attr is not None:
            expert_index, router_probs, router_logits = self.gate(torch.cat((x, attr),dim=-1))
        elif attr is not None:
            expert_index, router_probs, router_logits = self.gate(attr)
        else:
            expert_index, router_probs, router_logits = self.gate(x)


        route_prob_max, routes = torch.max(router_probs, dim=-1)

        # Get indexes of tokens going to each expert
        indexes_list = [torch.eq(routes, i).nonzero(as_tuple=True)[0] for i in range(self.n_experts)]

        # Initialize an empty tensor to store outputs
        x = x.reshape(-1, C)
        qkv = x.new_zeros(Bs*H*W, 3*C)

        # Capacity of each expert.
        # $$\mathrm{expert\;capacity} =
        # \frac{\mathrm{tokens\;per\;batch}}{\mathrm{number\;of\;experts}}
        # \times \mathrm{capacity\;factor}$$
        capacity = int(self.expert_capacity * Bs * H * W / self.n_experts)

        # Initialize an empty list of dropped tokens
        dropped = []
        # Only drop tokens if `drop_tokens` is `True`.
        if self.drop_tokens and self.training:
            # Drop tokens in each of the experts
            for i in range(self.n_experts):
                # Ignore if the expert is not over capacity
                if len(indexes_list[i]) <= capacity:
                    continue
                # Shuffle indexes before dropping
                indexes_list[i] = indexes_list[i][torch.randperm(len(indexes_list[i]))]
                # Collect the tokens over capacity as dropped tokens
                dropped.append(indexes_list[i][capacity:])
                # Keep only the tokens upto the capacity of the expert
                indexes_list[i] = indexes_list[i][:capacity]

        # Get outputs of the expert FFNs
        # expert_output = [self.experts[i](x[indexes_list[i], :]) for i in range(self.n_experts)]
        qkv_output = [self.qkv[i](x[indexes_list[i], :]) for i in range(self.n_experts)]

        # Assign to final output
        for i in range(self.n_experts):
            qkv[indexes_list[i], :] = qkv_output[i]
        if dropped:
            dropped_tensor = torch.cat(dropped)
            qkv[dropped_tensor, :] = torch.zeros_like(qkv[dropped_tensor, :], device=qkv.device)


        qkv = qkv.reshape(Bs, H, W, 3*C)

        moe_mask = qkv.new_zeros(Bs * H * W, 1)
        if dropped:
            moe_mask[dropped_tensor, :] = -torch.inf
        moe_mask = moe_mask.reshape(Bs, H, W, 1)

        if self.shift_size[-1] > 0:
            if len(self.window_size) == 3:
                shifted_qkv = torch.roll(qkv, shifts=(-self.shift_size[0], -self.shift_size[1], -self.shift_size[2]), dims=(1, 2, 3))
                shifted_moe_mask = torch.roll(moe_mask, shifts=(-self.shift_size[0], -self.shift_size[1], -self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                shifted_qkv = torch.roll(qkv, shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(1, 2))
                shifted_moe_mask = torch.roll(moe_mask, shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(1, 2))
        else:
            shifted_qkv = qkv
            shifted_moe_mask = moe_mask
            mask = None

        # qkv(): -> [batch_size*num_windows, Mh*Mw, 3 * total_embed_dim]
        # reshape: -> [batch_size*num_windows, Mh*Mw, 3, num_heads, embed_dim_per_head]
        # permute: -> [3, batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]

        qkv_windows = window_partition(shifted_qkv, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        moe_mask_windows = window_partition(shifted_moe_mask, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]

        qkv_windows = qkv_windows.reshape(-1, *self.total_window_size, 3*C)
        moe_mask_windows = moe_mask_windows.reshape(-1, *self.total_window_size, 1)
        B = qkv_windows.shape[0]
        if len(self.dilated_size) == 3:
            qkv_windows = window_partition(qkv_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], 3*C).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1, 3*C)
            moe_mask_windows = window_partition(moe_mask_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], 1).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1, 1)
        elif len(self.dilated_size) == 2:
            qkv_windows = window_partition(qkv_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], 3*C).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1], -1, 3*C)
            moe_mask_windows = window_partition(moe_mask_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], 1).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1], -1, 1)


        B_, N, _ = qkv_windows.shape

        qkv = qkv.reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)

        moe_mask_windows = moe_mask_windows.view(B_, 1, 1, N).expand(-1, self.num_heads, N, -1)


        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        q, k, v = qkv.unbind(0)  # make torchscript happy (cannot use tensor as tuple)

        q = self.position_enc(q.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)
        k = self.position_enc(k.reshape(-1, *self.window_size, C // self.num_heads)).reshape(B_, self.num_heads, -1, C // self.num_heads)

        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))


        if mask is not None:
            # mask: [nW, Mh*Mw, Mh*Mw]
            nW = mask.shape[0]  # num_windows
            # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
            # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N) + moe_mask_windows
            attn = self.softmax(attn)
        else:
            attn = attn.view(-1, self.num_heads, N, N) + moe_mask_windows
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        attn_windows = (attn @ v).transpose(1, 2).reshape(B_, N, C)


        if len(self.window_size) == 3:
            attn_windows = attn_windows.reshape(B, -1, N, C).permute(0, 2, 1, 3).reshape(
                                            -1, self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], C)
            attn_windows = window_reverse(attn_windows, self.dilated_size, *self.total_window_size)
        elif len(self.window_size) == 2:
            attn_windows = attn_windows.reshape(B, -1, N, C).permute(0, 2, 1, 3).reshape(
                                            -1, self.dilated_size[0]*self.dilated_size[1], C)
            attn_windows = window_reverse(attn_windows, self.dilated_size, 1, *self.total_window_size)
            
        shifted_x = window_reverse(attn_windows, self.total_window_size, T, H, W)

        if self.shift_size[0] > 0:
            if len(self.window_size) == 3:
                x_out = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1], self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                x_out = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1]), dims=(1, 2))
        else:
            x_out = shifted_x


        x_out = x_out.reshape(-1, C)
        final_output = x_out.new_zeros(Bs*H*W, C)
        expert_output = [self.proj[i](x_out[indexes_list[i], :]) for i in range(self.n_experts)]
        for i in range(self.n_experts):
            final_output[indexes_list[i], :] = expert_output[i]
        if dropped:
            final_output[dropped_tensor, :] = x[dropped_tensor, :]



        if self.is_scale_prob:
            # Multiply by the expert outputs by the probabilities $y = p_i(x) E_i(x)$
            final_output = final_output * route_prob_max.view(-1, 1)
        else:
            # Don't scale the values but multiply by $\frac{p}{\hat{p}} = 1$ so that the gradients flow
            # (this is something we experimented with).
            final_output = final_output * (route_prob_max / route_prob_max.detach()).view(-1, 1)
        x = final_output.reshape(Bs, H, W, C)

        x = self.proj_drop(x)
        z_loss = router_z_loss_func(router_logits=router_logits)
        balance_loss = load_balancing_loss_func(router_probs=router_probs, expert_indices=expert_index)

        return x, z_loss, balance_loss



class SD_attn_parallel(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, attn_drop=0., 
                 proj_drop=0., shift_size=[0, 0, 0], dilated_size=[1,1,1], use_cpu_initialization=True) -> None:
        super().__init__()
        self.dim = dim
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5
        
        self.dilated_size = dilated_size[-len(window_size):]
        self.window_size = window_size
        self.shift_size = shift_size
        self.total_window_size = [window_size[i] * dilated_size[i] for i in range(len(window_size))]
        world_size = mpu.get_tensor_model_parallel_world_size()
        self.num_attention_heads_per_partition = networks.megatron_utils.utils.divide(
            num_heads, world_size)

        if len(self.window_size) == 2:
            self.rope_quad = rope2(self.window_size, head_dim)
        elif len(self.window_size) == 3:
            self.rope_quad = rope3(self.window_size, head_dim)
       

        self.qkv = ColumnParallelLinear(dim, dim*3, bias=qkv_bias, gather_output=False, 
                                        async_tensor_model_parallel_allreduce=False,
                                        use_cpu_initialization=use_cpu_initialization)

        # self.attn_drop = nn.Dropout(attn_drop)

        self.proj = RowParallelLinear(dim, dim, input_is_parallel=True,
                                      use_cpu_initialization=use_cpu_initialization)
        # self.proj_drop = nn.Dropout(proj_drop)

        self.softmax = nn.Softmax(dim=-1)

        if len(window_size) == 2:
            self.position_enc = rope2(window_size, head_dim)
        elif len(window_size) == 3:
            self.position_enc = rope3(window_size, head_dim)


    def create_mask(self, x):
        # calculate attention mask for SW-MSA
        # 保证Hp和Wp是window_size的整数倍
        # Hp = int(np.ceil(H / self.window_size[0])) * self.window_size[0]
        # Wp = int(np.ceil(W / self.window_size[1])) * self.window_size[1]
        # 拥有和feature map一样的通道排列顺序，方便后续window_partition


        if len(self.window_size) == 3:
            _, T, H, W, _ = x.shape
            img_mask = torch.zeros((1, T, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            t_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            h_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], -self.shift_size[1]),
                        slice(-self.shift_size[1], None))
            w_slices = (slice(0, -self.window_size[2]),
                        slice(-self.window_size[2], 0),
                        slice(0, None))
            cnt = 0
            for t in t_slices:
                for h in h_slices:
                    for w in w_slices:
                        img_mask[:, t, h, w, :] = cnt
                        cnt += 1
        elif len(self.window_size) == 2:
            _, H, W, _ = x.shape
            img_mask = torch.zeros((1, H, W, 1), device=x.device)  # [1, Hp, Wp, 1]
            h_slices = (slice(0, -self.window_size[0]),
                        slice(-self.window_size[0], -self.shift_size[0]),
                        slice(-self.shift_size[0], None))
            w_slices = (slice(0, -self.window_size[1]),
                        slice(-self.window_size[1], 0),
                        slice(0, None))
            cnt = 0
            for h in h_slices:
                for w in w_slices:
                    img_mask[:, h, w, :] = cnt
                    cnt += 1

        mask_windows = window_partition(img_mask, self.total_window_size)  # [B, nW, Mt, Mh, Mw, C]
        mask_windows = mask_windows.reshape(-1, *self.total_window_size, 1)
        B_ = mask_windows.shape[0]
        if len(self.dilated_size) == 3:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], 1).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1)
        elif len(self.dilated_size) == 2:
            mask_windows = window_partition(mask_windows, self.dilated_size).reshape(B_, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], 1).permute(
                                        0, 2, 1, 3).reshape(B_*self.dilated_size[0]*self.dilated_size[1], -1)


        # mask_windows = window_partition(img_mask, self.window_size)  # [nW, Mh, Mw, 1]
        # if len(self.window_size) == 3:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1] * self.window_size[2])  # [nW, Mh*Mw]
        # elif len(self.window_size) == 2:
        #     mask_windows = mask_windows.view(-1, self.window_size[0] * self.window_size[1])  # [nW, Mh*Mw]
            
        attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)  # [nW, 1, Mh*Mw] - [nW, Mh*Mw, 1]
        # [nW, Mh*Mw, Mh*Mw]
        attn_mask = attn_mask.masked_fill(attn_mask != 0, -torch.inf).masked_fill(attn_mask == 0, float(0.0))
        return attn_mask

    
    def forward(self, x):
        """
        Args:
            x: input features with shape of (num_windows*B, Mh*Mw, C)
            mask: (0/-inf) mask with shape of (num_windows, Wh*Ww, Wh*Ww) or None
        """
        # [batch_size, Mt, Mh, Mw, total_embed_dim]
        T=1

        if len(self.window_size) == 2:
            Bs, H, W, C = x.shape
        elif len(self.window_size) == 3:
            Bs, T, H, W, C = x.shape

        if (self.shift_size[-1] == 0) or (self.total_window_size[-1] == W):
            mask = None
        else:
            mask = self.create_mask(x)

        # Initialize an empty tensor to store outputs
        x = x.reshape(Bs, -1, C)
        qkv, _ = self.qkv(x)
        qkv = qkv.reshape(Bs, H, W, -1)
        Bs, H, W, qkv_C = qkv.shape


        if self.shift_size[-1] > 0:
            if len(self.window_size) == 3:
                shifted_qkv = torch.roll(qkv, shifts=(-self.shift_size[0], -self.shift_size[1], -self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                shifted_qkv = torch.roll(qkv, shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(1, 2))
        else:
            shifted_qkv = qkv
            mask = None

        # qkv(): -> [batch_size*num_windows, Mh*Mw, 3 * total_embed_dim]
        # reshape: -> [batch_size*num_windows, Mh*Mw, 3, num_heads, embed_dim_per_head]
        # permute: -> [3, batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]

        qkv_windows = window_partition(shifted_qkv, self.total_window_size)  # [B * nW, Mt, Mh, Mw, C]
        # qkv_windows = qkv_windows.reshape(-1, *self.total_window_size, 3*C // self.world_size)
        B = qkv_windows.shape[0]
        if len(self.dilated_size) == 3:
            qkv_windows = window_partition(qkv_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], qkv_C).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], -1, qkv_C)
        elif len(self.dilated_size) == 2:
            qkv_windows = window_partition(qkv_windows, self.dilated_size).reshape(B, -1, 
                                        self.dilated_size[0]*self.dilated_size[1], qkv_C).permute(
                                        0, 2, 1, 3).reshape(B*self.dilated_size[0]*self.dilated_size[1], -1, qkv_C)


        B_, N, _ = qkv_windows.shape
        qkv = qkv.reshape(B_, N, 3, self.num_attention_heads_per_partition, qkv_C // self.num_attention_heads_per_partition // 3).permute(2, 0, 3, 1, 4)


        # [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        q, k, v = qkv.unbind(0)  # make torchscript happy (cannot use tensor as tuple)

        q = self.position_enc(q.reshape(-1, *self.window_size, qkv_C // self.num_attention_heads_per_partition//3)).reshape(B_, \
                                    self.num_attention_heads_per_partition, -1, qkv_C // self.num_attention_heads_per_partition//3)
        k = self.position_enc(k.reshape(-1, *self.window_size, qkv_C // self.num_attention_heads_per_partition//3)).reshape(B_, \
                                self.num_attention_heads_per_partition, -1, qkv_C // self.num_attention_heads_per_partition//3)

        # transpose: -> [batch_size*num_windows, num_heads, embed_dim_per_head, Mh*Mw]
        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, Mh*Mw]
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))


        if mask is not None:
            # mask: [nW, Mh*Mw, Mh*Mw]
            nW = mask.shape[0]  # num_windows
            # attn.view: [batch_size, num_windows, num_heads, Mh*Mw, Mh*Mw]
            # mask.unsqueeze: [1, nW, 1, Mh*Mw, Mh*Mw]
            attn = attn.view(B_ // nW, nW, self.num_attention_heads_per_partition, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_attention_heads_per_partition, N, N)
            attn = self.softmax(attn)
        else:
            attn = attn.view(-1, self.num_attention_heads_per_partition, N, N)
            attn = self.softmax(attn)

        # attn = self.attn_drop(attn)

        # @: multiply -> [batch_size*num_windows, num_heads, Mh*Mw, embed_dim_per_head]
        # transpose: -> [batch_size*num_windows, Mh*Mw, num_heads, embed_dim_per_head]
        # reshape: -> [batch_size*num_windows, Mh*Mw, total_embed_dim]
        attn_windows = (attn @ v).transpose(1, 2).reshape(B_, N, qkv_C//3)


        if len(self.window_size) == 3:
            attn_windows = attn_windows.reshape(B, -1, N, qkv_C//3).permute(0, 2, 1, 3).reshape(
                                            -1, self.dilated_size[0]*self.dilated_size[1]*self.dilated_size[2], qkv_C//3)
            attn_windows = window_reverse(attn_windows, self.dilated_size, *self.total_window_size)
        elif len(self.window_size) == 2:
            attn_windows = attn_windows.reshape(B, -1, N, qkv_C//3).permute(0, 2, 1, 3).reshape(
                                            -1, self.dilated_size[0]*self.dilated_size[1], qkv_C//3)
            attn_windows = window_reverse(attn_windows, self.dilated_size, 1, *self.total_window_size)
            
        shifted_x = window_reverse(attn_windows, self.total_window_size, T, H, W)

        if self.shift_size[0] > 0:
            if len(self.window_size) == 3:
                x = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1], self.shift_size[2]), dims=(1, 2, 3))
            elif len(self.window_size) == 2:
                x = torch.roll(shifted_x, shifts=(self.shift_size[0], self.shift_size[1]), dims=(1, 2))
        else:
            x = shifted_x
        
        Bs, H, W, _ = x.shape
        x = x.reshape(Bs, H*W, -1)
        x, _ = self.proj(x)
        x = x.reshape(Bs, H, W, -1)
        return x

