import torch
import torch.distributions as db
import torch.nn as nn
from torch.distributions.kl import kl_divergence
from torch.nn.functional import one_hot

from scvi.module.base import BaseModuleClass, auto_move_data
from scvi.nn import Encoder, FCLayers

from ._utils import _CE_CONSTANTS, DecoderGauss, DecoderNB, DrugNetwork


class CPAModule(BaseModuleClass):
    """
    CPA module using Gaussian/NegativeBinomial Likelihood

    Parameters
    ----------
        n_genes: int
        n_treatments: int
        covars_to_ncovars: dict
            Dictionary of covariates with keys as each covariate name and values as 
                number of unique values of the corresponding covariate
        n_latent: int
            Latent Dimension
        loss_ae: str
            Autoencoder loss (either "gauss" or "nb")
        doser: str
            # TODO: What is this
        autoencoder_width: int
        autoencoder_depth: int
        use_batch_norm: bool
        use_layer_norm: bool
        variational: bool
    """
    def __init__(self,
                 n_genes: int,
                 n_drugs: int,
                 covars_to_ncovars: dict,
                 n_latent: int = 256,
                 loss_ae="gauss",
                 doser_type="linear",
                 autoencoder_width=256,
                 autoencoder_depth=2,
                 adversary_width=128,
                 adversary_depth=3,
                 dosers_width: int = 64,
                 dosers_depth: int = 2,
                 use_batch_norm: bool = True,
                 use_layer_norm: bool = False,
                 variational: bool = False,
                 ):
        super().__init__()
        self.n_genes = n_genes
        self.n_drugs = n_drugs
        self.n_latent = n_latent
        self.loss_ae = loss_ae
        self.doser_type = doser_type
        self.ae_width = autoencoder_width
        self.ae_depth = autoencoder_depth
        self.dosers_width = dosers_width
        self.dosers_depth = dosers_depth
        self.adversary_width = adversary_width
        self.adversary_depth = adversary_depth
        self.use_batch_norm = use_batch_norm
        self.use_layer_norm = use_layer_norm
        self.variational = variational

        self.covars_to_ncovars = covars_to_ncovars

        self.variational = variational
        if variational:
            self.encoder = Encoder(
                n_genes,
                n_latent,
                var_activation=nn.Softplus(),
                n_hidden=autoencoder_width, 
                n_layers=autoencoder_depth,
                use_batch_norm=use_batch_norm,
                use_layer_norm=use_layer_norm,
            )
        else:
            self.encoder = FCLayers(
                n_in=n_genes,
                n_out=n_latent,
                n_hidden=autoencoder_width,
                n_layers=autoencoder_depth,
                use_batch_norm=use_batch_norm,
                use_layer_norm=use_layer_norm,
            )
        
        if self.loss_ae == 'nb':
            self.l_encoder = FCLayers(
                n_in=n_genes,
                n_out=1,
                n_hidden=autoencoder_width,
                n_layers=autoencoder_depth,
                use_batch_norm=use_batch_norm,
                use_layer_norm=use_layer_norm,
            )

        # Decoder components
        self.px_r = torch.nn.Parameter(torch.randn(n_genes))
        if loss_ae == "gauss":
            self.decoder = DecoderGauss(
                n_input=n_latent,
                n_output=n_genes,
                n_hidden=autoencoder_width,
                n_layers=autoencoder_depth,
                use_batch_norm=use_batch_norm,
                use_layer_norm=use_layer_norm,
                
            )
        else:
            self.decoder = DecoderNB(
                n_input=n_latent,
                n_output=n_genes,
                n_hidden=autoencoder_width,
                n_layers=autoencoder_depth,
                use_batch_norm=use_batch_norm,
                use_layer_norm=use_layer_norm,
            )

        # Embeddings
        # 1. Drug Network
        self.drug_network = DrugNetwork(n_drugs=self.n_drugs, 
                                        n_latent=self.n_latent, 
                                        doser_type=self.doser_type, 
                                        n_hidden=self.dosers_width, 
                                        n_layers=self.dosers_depth,
                                        )

        self.drugs_classifier = FCLayers(
            n_in=n_latent, 
            n_out=n_drugs,
            n_hidden=self.adversary_width,
            n_layers=self.adversary_depth,
            use_batch_norm=use_batch_norm,
            use_layer_norm=use_layer_norm,
        )

        # 2. Covariates Embedding
        self.covars_embedding = nn.ModuleDict(
            {
                key: torch.nn.Embedding(n_unique_cov_values, n_latent)
                for key, n_unique_cov_values in self.covars_to_ncovars.items()
            }
        )

        self.covars_classifiers = nn.ModuleDict(
            {
                key: FCLayers(n_in=n_latent, 
                              n_out=n_unique_cov_values,
                              n_hidden=self.adversary_width,
                              n_layers=self.adversary_depth,
                              use_batch_norm=use_batch_norm,
                              use_layer_norm=use_layer_norm)
                for key, n_unique_cov_values in self.covars_to_ncovars.items()
            }
        )

        self.adv_loss_covariates = nn.CrossEntropyLoss()
        self.adv_loss_drugs = nn.BCEWithLogitsLoss()

    def _get_inference_input(self, tensors):
        x = tensors[_CE_CONSTANTS.X_KEY] # batch_size, n_genes
        drugs_doses = tensors[_CE_CONSTANTS.PERTURBATIONS] # batch_size, n_drugs
        
        covars_dict = dict()
        # covars_onehot_dict = dict()
        for covar, n_covars in self.covars_to_ncovars.items():
            encoded_covars = tensors[covar] # (batch_size,)
            covars_dict[covar] = encoded_covars
            # if covar in self.covars_to_ncovars.keys():
            # val_oh = one_hot(encoded_covars.long().squeeze(), num_classes=n_covars)
            # else:
            #     val_oh = val
            # covars_onehot_dict[covar] = val_oh
        
        input_dict = dict(
            genes=x,
            drugs_doses=drugs_doses,
            covars_dict=covars_dict,
            # covars_onehot_dict=covars_onehot_dict,
        )
        return input_dict

    @auto_move_data
    def inference(
        self,
        genes,
        drugs_doses,
        covars_dict,
    ):
        # x_ = torch.log1p(x)
        x_ = genes
        if self.variational:
            qz_m, qz_v, latent_basal = self.encoder(x_)
            dist_qzbasal = db.Normal(qz_m, qz_v.sqrt())
        else:
            dist_qzbasal = None
            latent_basal = self.encoder(x_)
        
        if self.loss_ae == 'nb':
            library = self.l_encoder(x_)
        else:
            library = None

        latent_covariates = []
        for covar, _ in self.covars_to_ncovars.items():
            latent_covar_i = self.covars_embedding[covar](covars_dict[covar].long()) # batch_size, n_latent
            latent_covariates.append(latent_covar_i[None]) # 1, batch_size, n_latent
        latent_covariates = torch.cat(latent_covariates, 0).sum(0)  # Summing all covariates representations
        latent_treatment = self.drug_network(drugs_doses)
        latent = latent_basal + latent_covariates + latent_treatment

        return dict(
            latent=latent,
            latent_basal=latent_basal,
            dist_qz=dist_qzbasal,
            library=library,
            covars_dict=covars_dict,
        )

    def _get_generative_input(self, tensors, inference_outputs, **kwargs):
        input_dict = {}

        latent = inference_outputs["latent"]
        latent_basal = inference_outputs['latent_basal']
        if self.loss_ae == 'nb':
            library = inference_outputs["library"]
            input_dict["library"] = library

        covars_dict = dict()
        covars_onehot_dict = dict()
        for covar, n_covars in self.covars_to_ncovars.items():
            val = tensors[covar]
            covars_dict[covar] = val
            # if covar in self.covars_to_ncovars.keys():
            val_oh = one_hot(val.long().squeeze(), num_classes=n_covars)
            # else:
                # val_oh = val
            covars_onehot_dict[covar] = val_oh

        input_dict['latent'] = latent
        input_dict['latent_basal'] = latent_basal
        return input_dict

    @auto_move_data
    def generative(
        self,
        latent,
        latent_basal,
    ):
        drugs_pred = self.drugs_classifier(latent_basal)

        covars_pred = {}
        for covar in self.covars_to_ncovars.keys():
            covar_pred = self.covars_classifiers[covar](latent_basal)
            covars_pred[covar] = covar_pred

        if self.loss_ae == 'nb':
            dist_px = self.decoder(inputs=latent, px_r=self.px_r)
            return dict(
                dist_px=dist_px,
                drugs_pred=drugs_pred,
                covars_pred=covars_pred,
            )

        else:
            means, variances = self.decoder(inputs=latent)
            return dict(
                means=means,
                variances=variances,
                drugs_pred=drugs_pred,
                covars_pred=covars_pred,
            )

    def adversarial_loss(self, tensors, inference_outputs, generative_outputs):
        """Computes adversarial classification losses and regularizations"""
        drugs_doses = tensors[_CE_CONSTANTS.PERTURBATIONS]

        latent_basal = inference_outputs["latent_basal"]
        covars_dict = inference_outputs["covars_dict"]

        drugs_pred = generative_outputs["drugs_pred"]
        covars_pred = generative_outputs["covars_pred"]

        # Classification losses for different covariates
        adv_covars_loss = 0.0
        for covar in self.covars_to_ncovars.keys():
            adv_covars_loss += self.adv_loss_covariates(
                covars_pred[covar],
                covars_dict[covar].long().squeeze(-1),
            )
        
        # Classification loss for different drug combinations
        adv_drugs_loss = self.adv_loss_drugs(drugs_pred, drugs_doses.gt(0).float())
        adv_loss = adv_drugs_loss + adv_covars_loss

        # Penalty losses
        adv_penalty_covariates = 0.0
        for covar in self.covars_to_ncovars.keys():
            covar_penalty = (
                torch.autograd.grad(
                    covars_pred[covar].sum(), 
                    latent_basal, 
                    create_graph=True
                )[0].pow(2).mean()
            )
            adv_penalty_covariates += covar_penalty

        adv_penalty_treatments = (
            torch.autograd.grad(
                drugs_pred.sum(),
                latent_basal,
                create_graph=True,
            )[0].pow(2).mean()
        )
        adv_penalty = adv_penalty_covariates + adv_penalty_treatments

        return adv_loss, adv_penalty

    def loss(self, tensors, inference_outputs, generative_outputs):
        """Computes the reconstruction loss (AE) or the ELBO (VAE)"""
        x = tensors[_CE_CONSTANTS.X_KEY]
        # x = inference_outputs["x"]
        
        # Reconstruction loss & regularizations
        means = generative_outputs["means"]
        variances = generative_outputs["variances"]

        # log_px = dist_px.log_prob(x).sum(-1)
        # Compute reconstruction
        # reconstruction_loss = -log_px
        if self.loss_ae == "gauss":
            # TODO: Check with Normal Distribution
            # variance = dist_px.scale ** 2
            # mean = dist_px.loc
            term1 = variances.log().div(2)
            term2 = (x - means).pow(2).div(variances.mul(2))

            reconstruction_loss = (term1 + term2).mean()
            # term1 = variance.log().div(2)
            # term2 = (x - mean).pow(2).div(variance.mul(2))
            # reconstruction_loss = (term1 + term2).mean()

        # TODO: Add KL annealing if needed
        # if self.variational:
        #     dist_qz = inference_outputs["dist_qz"]
        #     dist_pz = db.Normal(
        #         torch.zeros_like(dist_qz.loc), torch.ones_like(dist_qz.scale)
        #     )
        #     kl_z = kl_divergence(dist_qz, dist_pz).sum(-1)
        #     loss = -log_px + kl_z
        # else:
        loss = reconstruction_loss

        return loss

    def get_expression(self, tensors, **inference_kwargs):
        """Computes gene expression means and std.

        Only implemented for the gaussian likelihood.

        Parameters
        ----------
        tensors : dict
            Considered inputs

        """
        _, generative_outputs, = self.forward(
            tensors,
            compute_loss=False,
            inference_kwargs=inference_kwargs,
        )
        if self.loss_ae == "gauss":
            mus = generative_outputs["means"]
            stds = generative_outputs["variances"]
            return mus, stds
        else:
            raise ValueError