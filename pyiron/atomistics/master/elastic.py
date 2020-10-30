# coding: utf-8
# Copyright (c) Max-Planck-Institut für Eisenforschung GmbH - Computational Materials Design (CM) Department
# Distributed under the terms of "New BSD License", see the LICENSE file.

from __future__ import print_function

import numpy as np
import scipy.constants
from pyiron.atomistics.master.parallel import AtomisticParallelMaster
from pyiron_base import JobGenerator
from sklearn.linear_model import LinearRegression
from collections import defaultdict
import warnings
import itertools

__author__ = "Sam Waseda"
__copyright__ = (
    "Copyright 2020, Max-Planck-Institut für Eisenforschung GmbH - "
    "Computational Materials Design (CM) Department"
)
__version__ = "1.0"
__maintainer__ = "Jan Janssen"
__email__ = "janssen@mpie.de"
__status__ = "production"
__date__ = "Oct 21, 2020"


eV_div_A3_to_GPa = (
    1e21 / scipy.constants.physical_constants["joule-electron volt relationship"][0]
)

def _fit_coeffs_with_stress(
        strain,
        stress,
        rotations,
        max_polynomial_order=None,
        fit_first_order=False,
    ):
    higher_terms = _get_higher_order_terms(
        strain,
        max_polynomial_order=max_polynomial_order,
        rotations=rotations,
        derivative=True,
    )
    strain = np.einsum('nik,mkl,njl->nmij',
                       rotations, strain, rotations).reshape(-1, 9)
    strain = strain[:, [0, 4, 8, 5, 2, 1]]
    strain[:,3:] *= 2
    if higher_terms is not None:
        strain = np.concatenate((strain, higher_terms), axis=-1)
    stress = np.einsum('nik,mkl,njl->nmij',
                       rotations, stress, rotations).reshape(-1, 9)
    stress = stress[:, [0, 4, 8, 5, 2, 1]]
    score = []
    coeff = []
    for ii in range(6):
        strain_tmp = strain.copy()
        strain_tmp[:, 6:] *= strain[:, ii, np.newaxis]
        reg = LinearRegression(
            fit_intercept=fit_first_order
        ).fit(strain_tmp, stress[:,ii])
        coeff.append(reg.coef_)
        score.append(reg.score(strain_tmp, stress[:,ii]))
    return np.array(coeff), np.array(score)

def _fit_coeffs_with_energies(
        strain,
        energy,
        volume,
        rotations,
        max_polynomial_order=None,
        fit_first_order=False,
    ):
    higher_terms = _get_higher_order_terms(
        strain,
        max_polynomial_order=max_polynomial_order,
        rotations=rotations,
        derivative=False,
    )
    strain_voigt = np.einsum('nik,mkl,njl->nmij',
                             rotations, strain, rotations).reshape(-1, 9)
    strain_voigt = strain_voigt[:, [0, 4, 8, 5, 2, 1]]
    strain_voigt[:,3:] *= 2
    energy = np.tile(energy, len(rotations))
    volume = np.tile(volume, len(rotations))
    strain = 0.5*np.einsum('ni,nj->nij', strain_voigt, strain_voigt)
    strain = np.triu(strain).reshape(-1, 36)
    strain = strain[:,np.sum(strain, axis=0)!=0]
    if higher_terms is not None:
        strain = np.concatenate((strain, higher_terms), axis=-1)
    if fit_first_order:
        strain = np.concatenate((strain, strain_voigt), axis=-1)
    strain = np.einsum('n,ni->ni', volume, strain)
    reg = LinearRegression().fit(strain, energy)
    score = reg.score(strain, energy)
    coeff = np.triu(np.ones((6,6))).flatten()
    coeff[coeff!=0] *= reg.coef_[:21]*eV_div_A3_to_GPa
    coeff = coeff.reshape(6,6)
    coeff = 0.5*(coeff+coeff.T)
    return coeff, score

def _get_linear_dependent_indices(strain_lst):
    """
    This function returns an array of booleans, which shows linearly dependent
    strain matrices. Strain matrices which have True along axis=0 are linearly
    dependent. Axis=1 should contain the total number of strain matrices.
    """
    s = np.array(strain_lst).reshape(-1, 9)
    norm = np.linalg.norm(s, axis=-1)
    sign = np.sign(np.sum(s, axis=-1))
    indices = np.round(
        np.einsum('ni,n->ni', s, sign/(norm+np.isclose(norm, 0))),
        decimals=8
    )
    indices = np.unique(indices, axis=0, return_inverse=True)[1]
    na = np.newaxis
    indices = np.unique(indices[~np.isclose(norm, 0)])[:,na]==indices[na,:]
    # 0-tensor gets a special treatment, since it's linearly dependent on all
    # terms (even though it virtually changes nothing)
    indices[:, np.isclose(norm, 0)] = True
    return indices

def _get_higher_order_terms(
        strain_lst,
        max_polynomial_order=None,
        rotations=None,
        derivative=False,
    ):
    """
    Returns a matrix containing higher order terms. axis=0 corresponds to the
    elastic tensors and axis=1 the higher order terms. From the number of
    linearly dependent terms the polynomial order is calculated internally
    -> up to 3: harmonic; 4 and 5: third degree etc.
    """
    strain_lst = np.array(strain_lst)
    indices = _get_linear_dependent_indices(strain_lst)
    # counts stands for the polynomial degree, starting from 1 meaning first
    # anharmonic contribution
    counts = np.sum(indices, axis=1)
    counts = np.floor(counts/2-0.75).astype(int)
    if max_polynomial_order is not None:
        counts[counts>max_polynomial_order-2] = max_polynomial_order-2
    counts[counts<0] = 0
    if sum(counts)==0: # No term gets more than harmonic part
        return None
    strain_higher_terms = np.zeros((len(strain_lst), np.sum(counts)))
    na = np.newaxis
    for cc, ind in zip(counts, indices):
        E = strain_lst[ind][
            np.linalg.norm(strain_lst[ind].reshape(-1, 9), axis=-1).argmax()]
        E /= np.linalg.norm(E)
        E = np.sum((E*strain_lst[ind]).reshape(-1, 9), axis=-1)
        if derivative:
            E = E[:,na]**(np.arange(cc)+1)[na,:]*np.sign(E)[:,na]
        else:
            E = E[:,na]**(np.arange(cc)+3)[na,:]
        starting_index = np.sum(np.any(strain_higher_terms!=0, axis=0))
        strain_higher_terms[ind, starting_index:starting_index+E.shape[1]] = E
    strain_higher_terms = np.einsum('n,ij->nij',
                                    np.ones(len(rotations)),
                                    strain_higher_terms)
    strain_higher_terms = strain_higher_terms.reshape(
        -1, strain_higher_terms.shape[-1]
    )
    return strain_higher_terms

def calc_elastic_tensor(
        strain,
        stress=None,
        energy=None,
        volume=None,
        rotations=None,
        return_score=False,
        max_polynomial_order=None,
        fit_first_order=False,
    ):
    """
    Calculate 6x6-elastic tensor from the strain and stress or strain and
    energy+volume.

    Rotations matrices can be added to take box symmetries into account (unit
    matrix can be added but does not have to be included in the list)

    Args:
        strain (numpy.ndarray): nx3x3 strain tensors
        stress (numpy.ndarray): nx3x3 stress tensors
        energy (numpy.ndarray): n energy values
        volume (numpy.ndarray): n volume values
        rotations (numpy.ndarray): mx3x3 rotation matrices
        return_score (numpy.ndarray): return regression score
            (cf. sklearn.linear_mode.LinearRegression)
    """
    if len(strain)==0:
        raise ValueError('Not enough points')
    if rotations is not None:
        rotations = np.append(np.eye(3), rotations).reshape(-1, 3, 3)
        _, indices = np.unique(
            np.round(rotations, 6), axis=0, return_inverse=True
        )
        rotations = rotations[np.unique(indices)]
    else:
        rotations = np.eye(3).reshape(1, 3, 3)
    if stress is not None and len(stress)==len(strain):
        coeff, score = _fit_coeffs_with_stress(
            strain=strain,
            stress=stress,
            rotations=rotations,
            max_polynomial_order=max_polynomial_order,
            fit_first_order=fit_first_order,
        )
    elif (energy is not None
          and volume is not None
          and len(energy)==len(strain)
          and len(energy)==len(volume)):
        coeff, score = _fit_coeffs_with_energies(
            strain=strain,
            energy=energy,
            volume=volume,
            rotations=rotations,
            max_polynomial_order=max_polynomial_order,
            fit_first_order=fit_first_order,
        )
    else:
        raise ValueError('Provide either strain and stress, or strain, energy '
                         + 'and volume of same length.')
    if return_score:
        return np.array(coeff)[:,:6], score
    else:
        return np.array(coeff)[:,:6]

def _get_random_symmetric_matrices(n):
    matrices = np.random.random((n, 3, 3))-0.5
    return matrices + np.einsum('nij->nji', matrices)


def get_strain(max_strain=0.05, n_set=10, polynomial_order=2, additional_points=0, normalize=False):
    """
        Args:
            max_strain (float): Maximum strain (for each component)
            n_set (int): Number of strain values to return
            polynomial_order (int): This value determines the number of
                linear-dependent strain values. For a polynomial order of two,
                there will be +-strain and for 3, there will be +-strain and
                +-0.5*strain etc.
            additional_points (int): Additional linear-dependent points
            normalize (bool): Whether to normalize the strain values. If True,
                the norm (Frobenius norm) of all outer most strain (i.e. the
                greatest strain within the linear-dependent terms) will be
                equal to max_strain

        Returns: numpy.ndarray of strains (n, 3, 3)
    """
    strain_lst = _get_random_symmetric_matrices(n_set)
    if normalize:
        strain_lst = np.einsum('nij,n->nij', strain_lst, 1/np.linalg.norm(strain_lst.reshape(-1, 9), axis=-1))
    strain_lst *= max_strain
    m = np.linspace(-1, 1, int(2*polynomial_order+2*additional_points-1))
    strain_lst = np.einsum('k,nij->nkij', m, strain_lst).reshape(-1, 9)
    return np.unique(strain_lst, axis=0).reshape(-1, 3, 3)

class _ElasticJobGenerator(JobGenerator):
    @property
    def parameter_list(self):
        parameter_lst = []
        for ii, epsilon in enumerate(self._job.input['strain_matrices']):
            basis = self._job.ref_job.structure.copy()
            basis.apply_strain(np.array(epsilon))
            parameter_lst.append([ii, basis])
        return parameter_lst

    def job_name(self, parameter):
        return "{}_{}".format(self._job.job_name, parameter[0]).replace(".", "_")

    @staticmethod
    def modify_job(job, parameter):
        job.structure = parameter[1]
        return job

class ElasticTensor(AtomisticParallelMaster):
    """
    Class to calculate the elastic tensor and isotropic elastic constants.

    Example:

    >>> job = pr.create_job('SomeAtomisticJob', 'atomistic')
    >>> job.structure = pr.create_structure('Fe', 'bcc', 2.83)
    >>> elastic = job.create_job('ElasticTensor', 'elastic')
    >>> elastic.run()

    Input parameters:

        min_num_measurements (int): Minimum number of measurements/simulations to be launched
        min_num_points (int): Minimum number of data points to fit data (number of measurements
            times number of symmetry operations)
        strain_matrices (numpy.ndarray): Strain tensors to be applied on simulation boxes (optional)
        rotations (numpy.ndarray): Rotation matrices for box symmetry
        use_symmetry (bool): Whether or not exploit box symmetry (ignored if `rotations` already specified)
        use_elements (bool): Whether or not respect chemical elements for box symmetry (ignored if `rotations`
            already specified)

    The default input parameters might not be chosen adequately. If you have a large
    computation power, increase `min_num_measurements`. At the same time, make
    sure to choose an orientation which maximizes the number symmetry operations.
    Also if the child job does not support pressure, better increase the number
    of measurements.
    """
    def __init__(self, project, job_name):
        super().__init__(project, job_name)
        self.__name__ = "ElasticTensor"
        self.__version__ = "0.1.0"

        self.input["min_num_measurements"] = (11, "minimum number of samples to be taken")
        self.input["min_num_points"] = (105, "minimum number of data points"
            + "(number of measurements will be min_num_points/len(rotations))")
        self.input["max_strain"] = (
            0.01,
            "relative volume variation around volume defined by ref_ham",
        )
        self.input['polynomial_order'] = 2
        self.input['additional_points'] = (
            0,
            'number of additional linear-dependent points to make anharmonic'
             + ' contribution more stable. It should not be larger than 0 if'
             + ' polynomial_order=2'
        )
        self.input['strain_matrices'] = (
            [], 
            'List of strain matrices (generated automatically if not set)'
        )
        self.input['use_symmetry'] = (True, 'Whether to consider box symmetries')
        self.input['rotations'] = (
            [],
            'List of rotation matrices (generated automatically if not set)'
        )
        self.input['normalize_magnitude'] = (
            False,
            'Whether or normalize magnitude, so that the Frobenius norm is '
            + 'always max_strain'
        )
        self.input['use_elements'] = (
            True,
            'Whether or not consider chemical elements for the symmetry'
            + ' analysis. Could be useful for SQS'
        )
        self.input['fit_first_order'] = (
            False,
            'Whether or not fit first order terms. In principle it should not'
            + ' be necessary, but might stabilize the calculation if the'
            + ' reference structure is not exactly in the zero pressure state.'
            + ' Setting this to True does not correct the second order terms'
        )
        self._job_generator = _ElasticJobGenerator(self)

    @property
    def _number_of_measurements(self):
        return int(max(
            self.input['min_num_measurements'],
            np.ceil(self.input['min_num_points']/max(len(self.input['rotations']), 1))
        ))

    def _get_rotation_matrices(self):
        rotations = self.ref_job.structure.get_symmetry(use_elements=self.input['use_elements'])['rotations']
        _, indices = np.unique(np.round(rotations, 6), axis=0, return_inverse=True)
        rotations = rotations[np.unique(indices)]
        self.input['rotations'] = rotations.tolist()

    def _create_strain_matrices(self):
        if self.input['use_symmetry'] and len(self.input['rotations'])==0:
            self._get_rotation_matrices()
        eps_lst = _get_random_symmetric_matrices(int(self._number_of_measurements))
        eps_lst *= self.input['max_strain']
        self.input['strain_matrices'] = get_strain(max_strain=self.input['max_strain'],
                                                   n_set=self._number_of_measurements,
                                                   polynomial_order=self.input['polynomial_order'],
                                                   additional_points=self.input['additional_points'],
                                                   normalize=self.input['normalize_magnitude']).tolist()

    def validate_ready_to_run(self):
        super().validate_ready_to_run()
        if len(self.input['strain_matrices'])==0:
            self._create_strain_matrices()
        if self.input['polynomial_order']<2:
            raise ValueError('Minimum polynomial order: 2')
        if self.input['polynomial_order']==2 and self.input['additional_points']>0:
            warnings.warn('Setting additional points for anharmonic calculations only increases the number of calculations')
        if self.input['polynomial_order']==2 and self.input['normalize_magnitude']:
            warnings.warn('Magnitude normalization could reduce accuracy in harmonic calculations')
        if self.input['polynomial_order']>2 and not self.input['normalize_magnitude']:
            warnings.warn('Not normalizing magnitude could destabilise fit procedure')
        if self.input['fit_first_order']:
            warnings.warn('First order fit does not correct the second order coefficients in the current implementation')

    def collect_output(self):
        if self.ref_job.server.run_mode.interactive:
            job = self.project_hdf5.inspect(self.child_ids[0])
            for key in ['energy_tot', 'energy_pot', 'pressures', 'volume']:
                if key in job["output/generic"].list_nodes():
                    self._output[key] = job["output/generic/{}".format(key)]
                else:
                    self._output[key] = []
        else:
            job_lst = [self.project_hdf5.inspect(job_id) for job_id in self.child_ids]
            self._output['id'] = self.child_ids
            for key in ['energy_tot', 'energy_pot', 'pressures', 'volume']:
                if all(key in job["output/generic"].list_nodes() for job in job_list):
                    self._output[key] = np.array([
                        job["output/generic/{}".format(key)][-1] for job in job_list
                    ])
                else:
                    self._output[key] = []
        energy = self._output['energy_tot']
        if len(self._output['energy_pot'])==len(self._output['volume']):
            energy = self._output['energy_pot']
        elastic_tensor, score = calc_elastic_tensor(
            strain=self.input['strain_matrices'],
            stress=-np.array(self._output['pressures']),
            rotations=self.input['rotations'],
            energy=energy,
            volume=self._output['volume'],
            return_score=True,
            fit_first_order=self.input['fit_first_order'],
            max_polynomial_order=self.input['polynomial_order'],
        )
        self._output['fit_score'] = score
        self._output['elastic_tensor'] = elastic_tensor
        with self.project_hdf5.open("output") as hdf5_out:
            for key, val in self._output.items():
                hdf5_out[key] = val



