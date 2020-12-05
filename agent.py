import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import cv2
import random
import os

from params import TRANSITION_CNN_LAYER_WT, DEVICE, ACTION_SET, OBSERVTAION_MATRIX

IF_PRINT = False
EPS = 1e-10

class POMDPAgent(nn.Module):

    def __init__(self, grid_size, n_obs, n_state):
        super(POMDPAgent, self).__init__()

        self.grid_size = grid_size # (w, h)

        ### Model Parameters ###
        # State Transition
        self.transition = nn.Conv2d(3, 3, 3, stride=1, padding=1).to(DEVICE)
        for name, param in self.transition.named_parameters():
            if name == 'weight':
                print(name)
                param.data=TRANSITION_CNN_LAYER_WT['weight'].to(DEVICE)
            if name == 'bias':
                print(name)
                param.data = torch.ones_like(param.data)*0
        # State Observation
        self.observation_matrix = OBSERVTAION_MATRIX.to(DEVICE)
        self.obs_bmm_matrix = (self.observation_matrix.T).repeat(grid_size[0]*grid_size[1], 1, 1).to(DEVICE)

        # State Estimate
        self.state_est = (torch.ones(grid_size[0], grid_size[1], 3)/3).to(DEVICE)
        

    def output_image(self, size=(1200,400)):
        img = self.state_est.data.cpu().numpy().squeeze()

        img_state0 = img[:,:,0]
        img_state1 = img[:,:,1]
        img_state2 = img[:,:,2]

        w, h = self.grid_size
        blank = np.zeros((h, int(w/20)))
        #img = np.concatenate((img_state0, blank, np.clip(img_state1 - img_state0, 0, 1), blank, img_state2), axis=1)
        img = np.concatenate((img_state0, blank, img_state1, blank, img_state2), axis=1)

        dim = size
        
        img_resized = cv2.resize(img, dim, interpolation = cv2.INTER_AREA)

        return img_resized

    def Bayesian_update(self, obs):
        '''
        Beysian recursive update using the following formula

                   B u_k
        u_k+1 = P --------
                   b u_k

        where
        obs: 
        '''

        ### Bayesian Posterior ###
        if IF_PRINT:    
            print('obs:', obs.size())
            print('state_est:', self.state_est.size())

        u_k = self.state_est
        u_k = u_k.reshape(-1, 3, 1) # reshape for batch matrix multiplication (BMM) 
        obs = obs.reshape(-1, 3, 1) # reshape for batch matrix multiplication (BMM)

        if IF_PRINT:    
            print('u_k', u_k.size())
            print('obs', obs.size())
            print('bmm_mat', self.obs_bmm_matrix.size())

        b = torch.bmm(self.obs_bmm_matrix, obs)
        Bu_k = b*u_k
        bu_k =  torch.sum(Bu_k, 1).unsqueeze(-1)
        Bu_over_bu = Bu_k / bu_k
        Bu_over_bu = Bu_over_bu.reshape(self.grid_size[0], self.grid_size[1], 3).permute(2, 0, 1).unsqueeze(0)

        ### State Transitoin ###
        prob_dist = self.transition(Bu_over_bu).squeeze()
        prob_dist = prob_dist / torch.sum(prob_dist, 0)
        prob_dist = prob_dist.permute(1,2,0)

        if IF_PRINT:
            print('Bu_over_bu', Bu_over_bu.size())
            print('prob_dist', prob_dist.size())
            print(prob_dist)

        self.state_est = prob_dist

        return self.state_est

from model import DynamicAutoEncoderNetwork


class DynamicAutoEncoder:

    def __init__(self, setting, grid_size, n_state, n_obs, encoding_dim, gru_hidden_dim):


        self.model = DynamicAutoEncoderNetwork(grid_size, n_state, n_obs, encoding_dim, gru_hidden_dim).to(DEVICE)
        self.optimizer = torch.optim.Adam(self.model.parameters(), setting['lr_optim_dynautoenc'], setting['betas_optim_dynautoenc'])

        self.encoding_dim = encoding_dim
        self.grid_size = grid_size
        self.n_state = n_state
        self.n_obs = n_obs
        self.gru_hidden_dim = gru_hidden_dim

        ### States ###
        self.u_k = F.softmax(torch.rand(self.grid_size[0], self.grid_size[1], n_state, 1),2).to(DEVICE)
        self.h_k = torch.rand(1, 1, self.gru_hidden_dim).to(DEVICE)

        

    def save_the_model(self, iteration):
        if not os.path.exists('./save/dynautoenc/'):
            os.makedirs('./save/dynautoenc/')
        f_name = 'dynautoenc_network_param_' +  str(iteration) + '_model.pth'
        torch.save(self.model.state_dict(), './save/dynautoenc/'+f_name)
        print('Model Saved')


    def load_the_model(self, iteration):
        f_path = './save/dynautoenc/dynautoenc_network_param_' +  str(iteration) + '_model.pth'
        self.model.load_state_dict(torch.load(f_path))
        print('Model Loaded')


    def step(self, obs, mask):
        self.model.eval()

        ### Likelihood ###
        O_T = F.softmax(self.model.W_obs_param,0).T # Transpose of the observation matrix
        O_T = O_T.unsqueeze(0).unsqueeze(0).repeat(self.grid_size[0], self.grid_size[1], 1, 1)
        y = obs.unsqueeze(-1).detach()
        b = torch.matmul(O_T, y)

        ### State Estimate ###
        Bu = b*self.u_k # Size : w x h x n_state x 1
        bu =  torch.sum(Bu, 2).unsqueeze(-1)
        Bu_over_bu = Bu / bu # Size : w x h x n_state x 1
        Bu_over_bu[Bu_over_bu != Bu_over_bu] = 0 

        ### Masking ###
        mask = mask.unsqueeze(-1).unsqueeze(-1)
        state_est_grid = self.u_k*(1-mask)
        state_est_grid = state_est_grid + Bu_over_bu*mask

        ### Encoding the Likelihood ###
        obs = obs.unsqueeze(-1).detach()
        x = self.model.encoder(obs.permute(3,2,0,1).contiguous())

        ### RNN Step ###
        h0 = self.h_k
        output, h_n = self.model.rnn_layer(x.unsqueeze(0), h0)
        self.h_k = output.detach()

        ### Decoding ###
        output = output[0].unsqueeze(-1).unsqueeze(-1)
        pred_state_est = self.model.decoder(output)
        pred_state_est = pred_state_est[:, :, :self.grid_size[0], :self.grid_size[1]] # Crop Image
        pred_state_est = F.softmax(pred_state_est, 1)
        pred_state_est = pred_state_est.permute(2, 3, 1, 0).contiguous()
        self.u_k = pred_state_est.detach()

        return state_est_grid
    
    def output_image(self, state_est_grid, size=(1200,400)):
        img = state_est_grid.data.cpu().numpy().squeeze()

        img_state0 = img[:,:,0]
        img_state1 = img[:,:,1]
        img_state2 = img[:,:,2]

        w, h = self.grid_size
        blank = np.zeros((h, int(w/20)))
        #img = np.concatenate((img_state0, blank, np.clip(img_state1 - img_state0, 0, 1), blank, img_state2), axis=1)
        img = np.concatenate((img_state0, blank, img_state1, blank, img_state2), axis=1)
        dim = size
    
        img_resized = cv2.resize(img, dim, interpolation = cv2.INTER_AREA)
        return img_resized

    
    def update(self, memory, n_batch, n_window):
        self.model.train()

        batch_obs_stream, batch_state_stream, batch_mask_stream = memory.sample(n_batch, n_window)
        batch_pred_obs_stream = []
        batch_tgt_obs_stream = []
        batch_tgt_mask_stream = []

        O = F.softmax(self.model.W_obs_param,0)
        O_np_val = O.data.cpu().numpy()
        O = O.unsqueeze(0).unsqueeze(0).repeat(self.grid_size[0], self.grid_size[1], 1, 1)
        O_bat = O.unsqueeze(0).repeat(n_window, 1, 1, 1, 1)

        ### Foward the observation through the model ###
        for i in range(n_batch):
            ### Encoding and Likelihood ###
            obs_stream = torch.stack(batch_obs_stream[i])
            batch_tgt_obs_stream.append(obs_stream.unsqueeze(-1))
            obs_stream = obs_stream.squeeze().permute(0, 3, 1, 2).contiguous()
            x_stream = self.model.encoder(obs_stream)
            

            ### RNN State Predictor ###
            h0 = torch.rand(1, 1, self.gru_hidden_dim).to(DEVICE)
            output, h_n = self.model.rnn_layer(x_stream.unsqueeze(0), h0)

            ### Decoding ###
            output = output.squeeze().unsqueeze(-1).unsqueeze(-1)
            pred_state_est_stream = self.model.decoder(output)
            pred_state_est_stream = pred_state_est_stream[:, :, :self.grid_size[0], :self.grid_size[1]] # Crop Image
            pred_state_est_stream = F.softmax(pred_state_est_stream, 1)
            pred_state_est_stream = pred_state_est_stream.permute(0,2,3,1).contiguous().unsqueeze(-1)
            pred_obs_stream = torch.matmul(O_bat, pred_state_est_stream)
            batch_pred_obs_stream.append(pred_obs_stream)

            ### Mask ###
            mask_stream = torch.stack(batch_mask_stream[i])
            batch_tgt_mask_stream.append(mask_stream.unsqueeze(-1).unsqueeze(-1))

        batch_pred_obs_stream = torch.stack(batch_pred_obs_stream)
        batch_tgt_obs_stream = torch.stack(batch_tgt_obs_stream)
        batch_tgt_mask_stream = torch.stack(batch_tgt_mask_stream)

        #### Translate one step the target for calculating loss in prediction
        tgt_mask_obs = batch_tgt_mask_stream[:, 1:, :, :, :]
        tgt_grid_obs = batch_tgt_obs_stream[:, 1:, :, :, :]
        pred_grid_obs = batch_pred_obs_stream[:, :-1, :, :, :]

        mask_tgt_grid_obs = tgt_grid_obs*tgt_mask_obs
        mask_pred_grid_obs = pred_grid_obs*tgt_mask_obs
        

        #### Cross Entropy Loss ###
        loss1 = torch.sum(-(mask_tgt_grid_obs*torch.log(mask_pred_grid_obs + EPS)+(1-mask_tgt_grid_obs)*torch.log(1-mask_pred_grid_obs + EPS)))/torch.sum(tgt_mask_obs)

        ### Shannon entropy loss ###
        p = pred_grid_obs
        log_p = torch.log(p)
        loss2 = -torch.mean(p*log_p)

        loss = loss1 #+ loss2

        ### Update Model ###
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()


        loss_val = loss.item()
        loss1_val = loss1.item()
        loss2_val = loss2.item()

        del batch_obs_stream, batch_state_stream, batch_pred_obs_stream, batch_tgt_obs_stream, loss, loss1, loss2
        torch.cuda.empty_cache()

        return loss_val, loss1_val, loss2_val, O_np_val



def render (window_name, image, wait_time):
    cv2.imshow(window_name, image)
    cv2.waitKey(wait_time)


def model_based_recursive_estimation():
    from environment import FireEnvironment

    env = FireEnvironment(50, 50)
    agent = POMDPAgent(grid_size = (env.map_width, env.map_height), n_obs=3, n_state=3)
    
    obs, state = env.reset()

    for i in range(5000):

        img_env   = env.output_image()
        img_agent = agent.output_image()

        render('env', img_env, 10)
        render('est', img_agent, 10)

        act = random.randrange(len(ACTION_SET))
        obs, state = env.step(ACTION_SET[act])

        state_est = agent.Bayesian_update(obs)





class Vehicle:
    def __init__(self, n_vehicle = 3, n_time_windows=500, grid_size=(64, 64)):

        self.grid_size = grid_size
        self.n_vehicle = n_vehicle
        self.n_time_windows = n_time_windows
        self.position_state = np.zeros(2)


    def full_mask(self):
        mask = np.ones(self.grid_size)

        dim = (400, 400)
        img_resized = cv2.resize(mask, dim, interpolation = cv2.INTER_AREA)
        mask = torch.FloatTensor(mask).to(DEVICE)

        return mask, img_resized


    def plan_a_trajectory(self, obs_grid_map=None, stat_est_map=None):
        '''
        For now, I generate a uniformly randon explorative trajectory
        '''

        '''

        MASK = torch.zeros(self.n_vehicle, self.grid_size[0], self.grid_size[1])

        U_delta_i = torch.randint(low=-1, high=1, size=(self.n_vehicle, self.n_time_windows))
        U_delta_j = torch.randint(low=-1, high=1, size=(self.n_vehicle, self.n_time_windows))

        I_cum_i = torch.clamp(torch.cumsum(U_delta_i, 1), 0, self.grid_size[0])
        J_cum_j = torch.clamp(torch.cumsum(U_delta_j, 1), 0, self.grid_size[1])

        IJ_cum_ij = torch.stack([I_cum_i, J_cum_j], 1)

        for k in range(self.n_vehicle):
            i_indice = IJ_cum_ij[k][0]
            j_indice = IJ_cum_ij[k][1]
            MASK[k][i_indice][j_indice] = 1

        mask = torch.clamp(torch.sum(MASK, 0), 0, 1)

        print(torch.sum(MASK))
        '''



        ACTION_SET = [[-1, 1],[0, 1],[1, 1],[-1, 0],[1, 0],[-1,-1],[0,-1],[1,-1]]

        mask = np.zeros(self.grid_size)

        for t in range(self.n_time_windows):
            act = random.randrange(len(ACTION_SET))
            self.position_state += np.array(ACTION_SET[act])
            self.position_state = np.clip(self.position_state, 0, self.grid_size[0]-1)

            pos_i = int(self.position_state[0])
            pos_j = int(self.position_state[1])
            mask[pos_i][pos_j] = 1

        dim = (400, 400)
        img_resized = cv2.resize(mask, dim, interpolation = cv2.INTER_AREA)
        mask = torch.FloatTensor(mask).to(DEVICE)

        return mask, img_resized









if __name__ == "__main__":
    import cv2
    size = (400,400)

    out = cv2.VideoWriter('project.avi',cv2.VideoWriter_fourcc(*'DIVX'), 15, size)
 
       
    vehicle = Vehicle()
    for i in range(100):
        visit_mask, img_resized = vehicle.plan_a_trajectory()
        #print(img_resized.shape)
        frame = img_resized #cv2.cvtColor(img_resized, cv2.COLOR_GRAY2BGR)
        out.write(frame)
        render('mask', img_resized, 1) 

    out.release()
    

    






