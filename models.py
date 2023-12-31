import tensorflow as tf
from tensorflow.keras import layers, Model, Sequential


class add_cls(layers.Layer):
    def __init__(self, dims):
        super(add_cls, self).__init__()
        self.dims = dims
        self.cls = self.add_weight('cls', (1, 1, dims))
        
    def build(self, inputs_shape):
        self.patchs = inputs_shape[1] * inputs_shape[2]
        self.pos_embed = self.add_weight('pos', (self.patchs, self.dims)) * 2e-2
        
    def call(self, inputs):
        b = tf.shape(inputs)[0]
        x = tf.reshape(inputs, (b, -1, self.dims)) + self.pos_embed
        return tf.concat([tf.tile(self.cls, (b, 1, 1)), x], 1)


class MultiHeadAttention(layers.Layer):
    def __init__(self, dims, heads):
        super(MultiHeadAttention, self).__init__()
        self.heads = heads
        self.dims = dims

        assert dims % self.heads == 0

        self.depth = dims // self.heads
        self.atte = layers.Attention()

    def split_heads(self, x, b):
        x = tf.reshape(x, (b, -1, self.heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, q, k, v=None, return_attention_scores=False):
        b = tf.shape(q)[0]
        q = self.split_heads(q, b)  # (b, heads, len_q, depth)
        k = self.split_heads(k, b)  # (b, heads, len_k, depth)
        
        if v is None:
            scaled_attention, weights = self.atte([q, k], return_attention_scores=True)
        else:
            v = self.split_heads(v, b)
            scaled_attention, weights = self.atte([q, k, v], return_attention_scores=True)

        scaled_attention = tf.transpose(scaled_attention, perm=[0, 2, 1, 3])  # (b, len_q, heads, depth)
        outputs = tf.reshape(scaled_attention, (b, -1, self.dims))  # (b, len_q, dims)

        if return_attention_scores:
            return outputs, weights
        else:
            return outputs


class Transformer(layers.Layer):
    def __init__(self, dims, heads):
        super(Transformer, self).__init__()
        self.dims = dims
        
        self.LN1 = layers.LayerNormalization()
        self.LN2 = layers.LayerNormalization()
        self.qkv = layers.Dense(3 * dims)
        self.mha = MultiHeadAttention(dims, heads)
        
        leakyReLU = layers.LeakyReLU()
        self.FFN = Sequential([
            layers.Dense(2 * dims, activation=leakyReLU),
            layers.Dense(dims, activation=leakyReLU),
        ])
        
    def call(self, inputs):
        x = self.LN1(inputs)
        
        qkv = self.qkv(x)
        q, k, v = tf.split(qkv, 3, -1)
        
        z = self.LN2(self.mha(q, k, v) + x)
        
        outputs = self.FFN(z) + z             
        return outputs
        
    def get_attention_weight(self, inputs):
        x = self.LN1(inputs)
        
        qkv = self.qkv(x)
        q, k, v = tf.split(qkv, 3, -1)

        z, w = self.mha(
            q, k, v, 
            return_attention_scores=True
        )
        z = self.LN2(z + x)
        outputs = self.FFN(z) + z 
        return outputs, w

    def get_Kz(self, inputs):
        x = self.LN1(inputs)
        
        qkv = self.qkv(x)
        q, k, v = tf.split(qkv, 3, -1)
        
        z = self.LN2(self.mha(q, k, v) + x)
        
        outputs = self.FFN(z) + z             
        return outputs, k


class model_vit(Model):
    def __init__(self, dims, num_classes, heads, num_layers=3):
        super(model_vit, self).__init__(name='vit')
        leakyReLU = layers.LeakyReLU()
        self.encoder = Sequential([
            layers.Conv2D(dims, 2, 2, activation=leakyReLU),
            layers.MaxPool2D(2),
            layers.Conv2D(dims, 2, 2, activation=leakyReLU),
            layers.MaxPool2D(2),
            add_cls(dims)
        ])
        self.mha = [Transformer(dims, heads) for _ in range(num_layers)]
        self.decoder = Sequential([
            layers.Dense(2 * dims, activation=leakyReLU),
            layers.Dense(num_classes)
        ])
    
    def call(self, inputs):
        z = self.encoder(inputs)
        for mha_i in self.mha:
            z = mha_i(z)
        return self.decoder(z[:, 0])
    
    def get_attention_weight(self, inputs):
        z = self.encoder(inputs)
        z_list = [z]
        w_list = []
        for mha_i in self.mha:
            z, w = mha_i.get_attention_weight(z)
            z_list.append(z)
            w_list.append(w)
        return z_list, w_list

    def get_Kz(self, inputs):
        z = self.encoder(inputs)
        Kz_list = []
        for mha_i in self.mha:
            z, Kz = mha_i.get_Kz(z)
            Kz_list.append(Kz)
        return Kz_list


class Creta(layers.Layer):
    def __init__(self, dims, heads, sigma=.1, lambd=.1):
        super(Creta, self).__init__()
        self.dims = dims
        
        self.LN1 = layers.LayerNormalization()
        self.LN2 = layers.LayerNormalization()
        self.U = self.add_weight('U', (dims, dims), initializer='orthogonal')
        self.mha = MultiHeadAttention(dims, heads)
        
        self.leakyReLU = layers.LeakyReLU()
        self.D = self.add_weight('D', (dims, dims), initializer='orthogonal')
        self.sigma = sigma
        self.lambd = lambd
        
    def call(self, inputs):
        x = self.LN1(inputs)
        
        z_l = x @ self.U
        z_half = self.LN2(self.mha(z_l, z_l) + x)

        z_next = self.sigma * ((z_half @ self.D - z_half) @ tf.transpose(self.D, (1, 0)) - self.lambd)
        return self.leakyReLU(z_next) + z_half
    
    def get_attention_weight(self, inputs):
        x = self.LN1(inputs)
        
        z_l = x @ self.U
        z_half, w = self.mha(z_l, z_l, return_attention_scores=True) 
        z_half = self.LN2(z_half + x)

        z_next = self.sigma * ((z_half @ self.D - z_half) @ tf.transpose(self.D, (1, 0)) - self.lambd)
        return self.leakyReLU(z_next) + z_half, w
    
    def get_Uz(self, inputs):
        x = self.LN1(inputs)
        
        z_l = x @ self.U
        z_half = self.LN2(self.mha(z_l, z_l) + x)

        z_next = self.sigma * ((z_half @ self.D - z_half) @ tf.transpose(self.D, (1, 0)) - self.lambd)
        return self.leakyReLU(z_next) + z_half, z_l


class model_crate(Model):
    def __init__(self, dims, num_classes, heads, num_layers=3):
        super(model_crate, self).__init__(name='vit')
        leakyReLU = layers.LeakyReLU()
        self.encoder = Sequential([
            layers.Conv2D(dims, 2, 2, activation=leakyReLU),
            layers.MaxPool2D(2),
            layers.Conv2D(dims, 2, 2, activation=leakyReLU),
            layers.MaxPool2D(2),
            add_cls(dims)
        ])
        self.mha = [Creta(dims, heads) for _ in range(num_layers)]
        self.decoder = Sequential([
            layers.Dense(2 * dims, activation=leakyReLU),
            layers.Dense(num_classes)
        ])
    
    def call(self, inputs):
        z = self.encoder(inputs)
        for mha_i in self.mha:
            z = mha_i(z)
        return self.decoder(z[:, 0])
    
    def get_attention_weight(self, inputs):
        z = self.encoder(inputs)
        z_list = [z]
        w_list = []
        for mha_i in self.mha:
            z, w = mha_i.get_attention_weight(z)
            z_list.append(z)
            w_list.append(w)
        return z_list, w_list

    def get_Uz(self, inputs):
        z = self.encoder(inputs)
        Uz_list = []
        for mha_i in self.mha:
            z, Uz = mha_i.get_Uz(z)
            Uz_list.append(Uz)
        return Uz_list
