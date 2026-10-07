from duma.pseudonyms import Pseudonyms


def test_each_person_keeps_one_code_and_the_names_come_back():
    names = Pseudonyms()
    assert names.code(10, "Ana Peña") == "ATLETA_01" and names.code(11, "Luis Ruiz") == "ATLETA_02"
    assert names.code(10, "Ana Peña") == "ATLETA_01"
    assert names.restore("ATLETA_02 va mejor que ATLETA_01; ATLETA_99 no existe.") == "Luis Ruiz va mejor que Ana Peña; ATLETA_99 no existe."
    assert names.athlete_id(" atleta_02 ") == 11 and names.athlete_id("Ana Peña") is None


def test_the_map_survives_being_saved_with_the_same_codes():
    names = Pseudonyms()
    names.code(10, "Ana Peña")
    names.code(11, "Luis Ruiz")
    again = Pseudonyms.from_list(names.to_list())
    assert again.restore("ATLETA_01 y ATLETA_02") == "Ana Peña y Luis Ruiz" and again.code(12, "Eva") == "ATLETA_03"
