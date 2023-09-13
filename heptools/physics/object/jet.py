from ...aktools import (FieldLike, add_arrays, foreach, get_field, or_arrays,
                        where)
from ._utils import PhysicsObjectError, register_behavior, typestr
from .vector import (DiLorentzVector, H, _Pair_LorentzVector,
                     _Plot_DiLorentzVector, _Plot_LorentzVector)


@register_behavior
class DiJet(DiLorentzVector):
    ...

@register_behavior
class ExtendedJet(DiLorentzVector):
    def _unique_field(self, field: FieldLike = ()):
        constituents = self.constituents
        jets = foreach(constituents.Jet)
        p = add_arrays(*(get_field(jet, field) for jet in jets))
        others = set(constituents.fields) - {'Jet'}
        for other in others:
            objs = foreach(constituents[other])
            for obj in objs:
                p = where(p + get_field(obj, field),
                          (or_arrays(*(obj.jetIdx == jet.index for jet in jets)), p))
        return p

    @property
    def _p(self):
        return self._unique_field()

    @property
    def st(self):
        return self._unique_field('pt')

    # TODO count

def _type_check_extended_jet(ps):
    type_check = {'Jet', 'DiJet', 'ExtendedJet'}
    for p in ps:
        if typestr(p) in type_check:
            return
    raise PhysicsObjectError(f'expected at least one of {type_check} (got [{", ".join(typestr(p) for p in ps)}])')

class _Pair_Jet(_Pair_LorentzVector):
    name = 'DiJet'
    type_check = {'Jet', 'DiJet'}

class _Extend_Jet(_Pair_LorentzVector):
    name = 'ExtendedJet'
    type_check = _type_check_extended_jet

class _Plot_Jet(_Plot_LorentzVector):
    deepjet_b   = H((100, 0, 1, ('btagDeepFlavB', 'DeepJet $b$')))
    deepjet_c   = H((100, 0, 1, ('btagDeepFlavCvL', 'DeepJet $c$ vs $uds+g$')),
                    (100, 0, 1, ('btagDeepFlavCvB', 'DeepJet $c$ vs $b$')))
    id_pileup   = H(([0b000, 0b100, 0b110, 0b111], ('puId', 'Pileup ID')))
    id_jet      = H(([0b000, 0b010, 0b110], ('jetId', 'Jet ID')))

class Jet:
    pair        = _Pair_Jet.create
    extend      = _Extend_Jet.create
    plot        = _Plot_Jet
    plot_pair   = _Plot_DiLorentzVector